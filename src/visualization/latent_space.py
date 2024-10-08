from functools import partial
import jax
from jax import Array, debug, jit, random
import jax.numpy as jnp
from jsrm.systems.pendulum import normalize_joint_angles
import matplotlib.pyplot as plt
import numpy as onp
from pathlib import Path
import tensorflow as tf
from tqdm import tqdm
from typing import Any, Callable, Dict, List, Optional, Tuple
import warnings

from src.structs import TaskCallables, TrainState


def visualize_mapping_from_configuration_to_latent_space(
    eval_ds: tf.data.Dataset,
    state: TrainState,
    task_callables: TaskCallables,
    rng: Optional[Array] = None,
):
    q_ss = None
    z_pred_ss = None
    num_batches = len(eval_ds)  # number of dataset samples

    # jit the forward function
    forward_fn = jit(
        partial(
            task_callables.forward_fn,
            nn_params=state.params,
            rng=rng,
        )
    )

    for batch_idx, batch in (pbar := tqdm(enumerate(eval_ds.as_numpy_iterator()))):
        pbar.set_description(
            f"Plotting latent space: processing batch {batch_idx + 1} / {num_batches}"
        )
        preds = forward_fn(batch)
        q_bt = batch["x_ts"][..., : batch["x_ts"].shape[-1] // 2]

        if "q_ts" in preds.keys():
            z_pred_bt = preds["q_ts"]
        elif "q_static_ts" in preds.keys():
            z_pred_bt = preds["q_static_ts"]
        else:
            raise ValueError(
                "Cannot find the predicted latent space variables in the predictions."
            )

        if task_callables.system_type == "pendulum":
            # normalize configuration variables
            q_bt = normalize_joint_angles(q_bt)

        if batch_idx == 0:
            q_ss = jnp.zeros((num_batches,) + q_bt.shape)
            z_pred_ss = jnp.zeros((num_batches,) + z_pred_bt.shape)

        q_ss = q_ss.at[batch_idx].set(q_bt)
        z_pred_ss = z_pred_ss.at[batch_idx].set(z_pred_bt)

    q_ss = q_ss.reshape((-1, q_bt.shape[-1]))
    z_pred_ss = z_pred_ss.reshape((-1, z_pred_bt.shape[-1]))

    # number of configuration variables
    n_q = q_ss.shape[-1]
    n_z = z_pred_ss.shape[-1]

    fig, axes = plt.subplots(
        nrows=n_q, ncols=1, num="Mapping from configuration to latent space"
    )
    if type(axes) != onp.ndarray:
        axes = onp.array([axes])
    for q_idx in range(n_q):
        ax = axes[q_idx]
        for z_idx in range(n_z):
            ax.plot(
                q_ss[:, q_idx],
                z_pred_ss[:, z_idx],
                linestyle="None",
                marker=".",
                label=rf"$z_{z_idx}$",
            )
        ax.set_xlabel(f"$q_{q_idx}$")
        ax.set_ylabel("$z$")
        ax.legend()
    plt.tight_layout()
    plt.show()
