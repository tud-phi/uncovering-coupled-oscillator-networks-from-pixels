from datetime import datetime
import flax.linen as nn
from jax import random
import jax.numpy as jnp
import jsrm
from jsrm.systems.pendulum import normalize_joint_angles
import matplotlib.pyplot as plt
from pathlib import Path
import tensorflow as tf

from src.models.autoencoders.convnext import ConvNeXtAutoencoder
from src.models.autoencoders.simple_cnn import Autoencoder
from src.models.autoencoders.vae import VAE
from src.tasks import autoencoding
from src.training.dataset_utils import load_dataset, load_dummy_neural_network_input
from src.training.loops import run_eval
from src.training.train_state_utils import restore_train_state
from src.visualization.latent_space import (
    visualize_mapping_from_configuration_to_latent_space,
)

# prevent tensorflow from loading everything onto the GPU, as we don't have enough memory for that
tf.config.experimental.set_visible_devices([], "GPU")

# initialize the pseudo-random number generator
seed = 0
rng = random.PRNGKey(seed=seed)
tf.random.set_seed(seed=seed)

system_type = "cc"
ae_type = "beta_vae"

if system_type == "cc":
    latent_dim = 1
elif system_type == "pcc_ns-2":
    latent_dim = 2
elif system_type == "pcc_ns-3":
    latent_dim = 3
else:
    raise ValueError(f"Unknown system type: {system_type}!")

conv_strides = (1, 1)
norm_layer = nn.LayerNorm

if ae_type == "wae":
    ckpt_timestamp = "2023-09-12_13-52-21"
    loss_weights = dict(mse_q=0.0, mse_rec=1.0, mmd=1.0)
elif ae_type == "beta_vae":
    if system_type == "cc":
        # Normal Beta-VAE:
        # ckpt_timestamp = "2023-09-15_15-23-19"
        # With time alignment loss:
        ckpt_timestamp = "2023-09-24_14-41-33"
    elif system_type == "pcc_ns-2":
        ckpt_timestamp = "2023-09-12_19-10-56"
    else:
        raise NotImplementedError
    loss_weights = dict(mse_q=0.0, mse_rec=1.0, beta=1.0)
elif ae_type == "triplet":
    ckpt_timestamp = "2023-09-19_19-11-45"
    loss_weights = dict(mse_q=0.0, mse_rec=1.0, triplet=1e0)
else:
    if system_type == "cc":
        ckpt_timestamp = "2023-09-16_11-54-15"
    elif system_type == "pcc_ns-2":
        ckpt_timestamp = "2023-09-12_19-35-14"
    else:
        raise NotImplementedError
    loss_weights = dict(mse_q=1.0, mse_rec=1.0)

ckpt_dir = ckpt_dir = (
    Path("logs").resolve() / f"{system_type}_autoencoding" / ckpt_timestamp
)
batch_size = 10

if __name__ == "__main__":
    datasets, dataset_info, dataset_metadata = load_dataset(
        f"planar_pcs/{system_type}_64x64px",
        seed=seed,
        batch_size=batch_size,
        normalize=True,
        grayscale=True,
    )
    train_ds, val_ds, test_ds = datasets["train"], datasets["val"], datasets["test"]

    # dimension of the latent space
    n_q = train_ds.element_spec["x_ts"].shape[-1] // 2
    # image shape
    img_shape = train_ds.element_spec["rendering_ts"].shape[-3:]

    # initialize the model
    if ae_type == "beta_vae":
        nn_model = VAE(
            latent_dim=latent_dim,
            img_shape=img_shape,
            strides=conv_strides,
            norm_layer=norm_layer,
        )
    else:
        nn_model = Autoencoder(
            latent_dim=latent_dim,
            img_shape=img_shape,
            strides=conv_strides,
            norm_layer=norm_layer,
        )

    # call the factory function for the autoencoding task
    task_callables, metrics_collection_cls = autoencoding.task_factory(
        system_type,
        nn_model,
        loss_weights=loss_weights,
        ae_type=ae_type,
    )

    # load the neural network dummy input
    nn_dummy_input = load_dummy_neural_network_input(test_ds, task_callables)
    # load the training state from the checkpoint directory
    state = restore_train_state(
        rng, ckpt_dir, nn_model, nn_dummy_input, metrics_collection_cls
    )

    print("Run testing...")
    state, test_history = run_eval(test_ds, state, task_callables)

    test_metrics = state.metrics.compute()
    print(
        "\n"
        f"Final test metrics: "
        f"rmse_q={test_metrics['rmse_q']:.4f}, "
        f"rmse_rec={test_metrics['rmse_rec']:.4f}, "
    )

    visualize_mapping_from_configuration_to_latent_space(
        test_ds, state, task_callables, rng=rng
    )

    test_batch = next(test_ds.as_numpy_iterator())
    test_preds = task_callables.forward_fn(test_batch, state.params, rng=rng)

    # try interpolating between two latent vectors
    img_gt1 = test_batch["rendering_ts"][1, 0]
    img_gt2 = test_batch["rendering_ts"][1, -1]
    img_bt = jnp.stack([img_gt1, img_gt2])
    # two latent vectors
    z_pred_bt = nn_model.apply({"params": state.params}, img_bt, method=nn_model.encode)
    # interpolate 10 points between the two latent vectors
    z_interp_bt = jnp.linspace(z_pred_bt[0], z_pred_bt[1], 10)
    input_decoder = z_interp_bt
    img_rec_bt = nn_model.apply(
        {"params": state.params}, input_decoder, method=nn_model.decode
    )
    # unnormalize the images to the range [0, 255]
    img_rec_bt_unnorm = (128 * (1.0 + img_rec_bt)).astype(jnp.uint8)

    fig, axes = plt.subplots(nrows=1, ncols=img_rec_bt.shape[0], figsize=(18, 4))
    interpolation_plts = []
    for i in range(len(axes)):
        axes[i].set_title("z = " + str(z_interp_bt[i]), fontdict={"fontsize": 10})
        interpolation_plts.append(
            axes[i].imshow(img_rec_bt_unnorm[i], vmin=0, vmax=255)
        )
    plt.suptitle("Interpolation between two latent vectors")
    plt.show()

    for i in range(test_batch["x_ts"].shape[0]):
        print("test sample:", i, "latent variable z:", test_preds["q_ts"][i, 0])
        img_gt = (128 * (1.0 + test_batch["rendering_ts"][i, 0])).astype(jnp.uint8)
        img_rec = (128 * (1.0 + test_preds["img_ts"][i, 0])).astype(jnp.uint8)

        fig, axes = plt.subplots(nrows=1, ncols=2, figsize=(10, 5))
        img_gt_plot = axes[0].imshow(img_gt, vmin=0, vmax=255)
        plt.colorbar(img_gt_plot, ax=axes[0])
        axes[0].set_title("Original")
        img_rec_plot = axes[1].imshow(img_rec, vmin=0, vmax=255)
        plt.colorbar(img_rec_plot, ax=axes[1])
        axes[1].set_title("Reconstruction")
        plt.show()
