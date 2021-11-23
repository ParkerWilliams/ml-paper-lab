import torch
import einops
from torch.utils.data import DataLoader, Dataset
from copy import deepcopy
from typing import List, Dict, Tuple

from paperlab.core import Config, evaluate_loss, wrap_data
from .models import MultiHeadAttention, ViTClassifier
from .data import get_data


sample_params = {
    'image_size': (32, 32),
    'patch_size': (8, 8),
    'num_channel': 3,
    'pool': 'cls',
    'num_class': 10,

    'transformer.depth': 4,
    'transformer.dim': 64,
    'transformer.dropout': 0.,
    'transformer.emb_dropout': 0.,
    'transformer.num_head': 4,
    'transformer.dim_head': 32,
    'transformer.dim_mlp': 32,

    'learning.batch_size': 16,
    'learning.lr': 1e-3,
    'learning.num_epoch': 4,
    'learning.early_stop_patience': 5,
}

sample_config = Config(**sample_params)
MOVING_DECAY = 0.9
EPS = 1e-5

def evaluate_accuracy(model, dataloader):
    labels, preds = [], []
    with torch.no_grad():
        for data in dataloader:
            if torch.cuda.is_available():
                data = wrap_data(data)

            image, label = data
            preds.append(model.pred(image))
            labels.append(label)

    acc_score = torch.sum(torch.cat(preds, dim=0) == torch.cat(labels, dim=0)) / len(dataloader.dataset)
    return acc_score.item()


def train(config):
    """
    train a Vision Transformer Image Classifier
    :param config:
    :return: the trained ViT Model
    """
    model = ViTClassifier(
        num_class=config.num_class,
        pool=config.pool,
        image_size=config.image_size,
        patch_size=config.patch_size,
        num_channel=config.num_channel,
        depth=config.transformer.depth,
        dim=config.transformer.dim,
        dropout=config.transformer.dropout,
        emb_dropout=config.transformer.emb_dropout,
        num_head=config.transformer.num_head,
        dim_head=config.transformer.dim_head,
        dim_mlp=config.transformer.dim_mlp,
    )

    num_params = sum(p.numel() for p in model.parameters())
    print(f"number of model parameter: {num_params}")

    if torch.cuda.is_available():
        model = model.cuda()

    optimizer = torch.optim.Adam(params=model.parameters(), lr=config.learning.lr)
    scheduler = torch.optim.lr_scheduler.LinearLR(optimizer=optimizer,
                                                  start_factor=1.,
                                                  end_factor=0.1,
                                                  total_iters=config.learning.num_epoch
                                                  )

    train_dataset, dev_dataset = get_data()
    train_dataloader = DataLoader(train_dataset,
                                  batch_size=config.learning.batch_size,
                                  shuffle=True)

    dev_dataloader = DataLoader(dev_dataset,
                                batch_size=config.learning.batch_size)

    step = 0
    moving_avg_loss = 0
    best_dev_loss, best_dev_score, best_model_state = float('inf'), float('-inf'), deepcopy(model.state_dict())
    patience_cnt = 0

    for _ in range(config.learning.num_epoch):
        for data in train_dataloader:
            data = wrap_data(data) if torch.cuda.is_available() else data

            step += 1
            optimizer.zero_grad()
            loss = model.compute_loss(data)
            loss.backward()
            optimizer.step()

            if step == 1:
                moving_avg_loss = loss.detach().data.item()
            else:
                moving_avg_loss = (1 - MOVING_DECAY) * loss.detach().data.item() + MOVING_DECAY * moving_avg_loss

            if step % 1000 == 0:
                print(f"step-{step} training_loss: {moving_avg_loss:.4f}")

        scheduler.step()
        dev_loss = evaluate_loss(model, dev_dataloader)
        dev_score = evaluate_accuracy(model, dev_dataloader)

        print(f"step-{step}: dev_loss: {dev_loss:.4f}, dev_acc: {dev_score:.4f}")
        if dev_score > best_dev_score + EPS:
            best_model_state = deepcopy(model.state_dict())
            best_dev_score = dev_score

        if dev_loss < best_dev_loss - EPS:
            patience_cnt = 0
            best_dev_loss = dev_loss
        else:
            patience_cnt += 1

        if patience_cnt >= config.learning.early_stop_patience:
            print(f"dev_loss doesnt descent in {config.learning.early_stop_patience} epochs, halt the training process.")
            break

    model.load_state_dict(best_model_state)
    return model


def get_attention_distance(model: ViTClassifier, dataloader: DataLoader) -> torch.Tensor:
    if torch.cuda.is_available():
        model = model.cuda()

    # enable cache so that we can store the attention maps across all layers for each image
    for module in model.modules():
        if isinstance(module, MultiHeadAttention):
            module.enable_cache()

    # run transformer
    with torch.no_grad():
        for image, _ in dataloader:
            if torch.cuda.is_available():
                image = wrap_data(image)

            model.transformer_encoder(image)

    # retrieve the cached attention map
    attn_maps: List[torch.Tensor] = []
    for module in model.modules():
        if isinstance(module, MultiHeadAttention):
            # attn_maps elem size: [data_size, num_head, num_patch, num_patch]
            attn_maps.append(torch.cat(module.cache['attn_map'], dim=0))
            module.disable_cache()
            module.clear_cache()

    _, _, height, width = image.shape
    ph, pw = model.transformer_encoder.patch_height, model.transformer_encoder.patch_width
    nh, nw = height // ph, width // pw

    num_layer, num_head = len(attn_maps), attn_maps[0].shape[1]

    def _pixel_dist_matrix(h, w):
        # the euclid distance between two pixels (h1, w1) and (h2, w2)
        dist = torch.empty(size=(h, w, h, w))
        xx, yy = torch.meshgrid(torch.arange(h), torch.arange(w), indexing='ij')
        for x in range(h):
            for y in range(w):
                # the distance between (x, y) and all other pixels, shape: [h, w]
                d = torch.sqrt((x - xx) ** 2 + (y - yy) ** 2)
                dist[x, y] = d

        return dist
    pixel_distance = _pixel_dist_matrix(height, width)

    mean_attention_distance = torch.empty((num_layer, num_head))
    for i in range(num_layer):
        for j in range(num_head):
            # the attention pixel (h1, w1) attended to (h2, w2), shape: [data_size, height, width, height, width]
            head_attn_pixel = einops.repeat(attn_maps[i][:, j, 1:, 1:],
                                            'b (nhx nwx) (nhy nwy) -> b (nhx phx) (nwx pwx) (nhy phy) (nwy pwy)',
                                             nhx=nh, nwx=nw, nhy=nh, nwy=nw,
                                             phx=ph, pwx=pw, phy=ph, pwy=pw)
            # shape: [data_size, height, width, height, width]
            weighted_distance = head_attn_pixel * torch.unsqueeze(pixel_distance, dim=0)
            mean_attention_distance[i, j] = torch.mean(torch.sum(weighted_distance, dim=(-1, -2)))

    return mean_attention_distance


def get_attention_maps(model: ViTClassifier, dataloader: DataLoader) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    get the attention maps over the images in the dataloader
    :param model:
    :param dataloader:
    :return: tuple (attn_map_pixel, images)
                shape: [data_size, height, width], [data_size, num_channel, height, width]
    """
    if torch.cuda.is_available():
        model = model.cuda()

    # enable cache so that we can store the attention maps across all layers for each image
    for module in model.modules():
        if isinstance(module, MultiHeadAttention):
            module.enable_cache()

    # let transformer process images
    images = []
    for image, _ in dataloader:
        if torch.cuda.is_available():
            image = wrap_data(image)

        with torch.no_grad():
            model.transformer_encoder(image)

        images.append(image)
    images = torch.cat(images, dim=0)  # [data_size, num_channel, height, width]

    # retrieve the cached attention map
    attn_maps = []
    for module in model.modules():
        if isinstance(module, MultiHeadAttention):
            # average attention over each head, shape: [data_size, num_patch, num_patch]
            avg_attn_map = torch.mean(torch.cat(module.cache['attn_map'], dim=0), dim=1)
            attn_maps.append(avg_attn_map)
            module.disable_cache()
            module.clear_cache()

    rollout = attn_rollout(attn_maps)  # [data_size, num_patch, num_patch]
    # get attention map for [cls] to each input patch token, and repeat patch tensor to pixel
    # [data_size, height, width]
    _, _, height, width = images.shape
    ph, pw = model.transformer_encoder.patch_height, model.transformer_encoder.patch_width
    nh, nw = height // ph, width // pw
    attn_map_pixel = einops.repeat(rollout[:, 0, 1:],
                                   'b (nh nw) -> b (nh ph) (nw pw)',
                                   nh=nh, nw=nw,
                                   ph=ph, pw=pw)

    return attn_map_pixel, images


def attn_rollout(attn_matrices: List[torch.Tensor]):
    """
    get the attention flow of each output unit at the last layer to each input token at the first layer in transformer 
    based on the attention rollout algorithm described in `Quantifying Attention Flow in Transformers`
    :param attn_matrices: the attention matrix at each layer in transformer
            element shape: [batch_size, num_token, num_token]
    :return: shape: [batch_size, num_token, num_token]
    """
    b, n, _ = attn_matrices[0].shape
    rollout = einops.repeat(torch.eye(n), 'n m -> b n m', b=b)
    for attn in map(lambda x: 0.5 * x + 0.5 * torch.eye(n), attn_matrices):
        rollout = torch.einsum('bij, bjk -> bik', rollout, attn)
    return rollout