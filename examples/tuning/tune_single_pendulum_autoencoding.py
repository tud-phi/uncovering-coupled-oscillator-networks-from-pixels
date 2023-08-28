from datetime import datetime
import dill
from jax import random
from jax import config as jax_config
import jax.numpy as jnp
from jsrm.systems.pendulum import normalize_joint_angles
import logging
from pathlib import Path
import optuna
import tensorflow as tf

from src.neural_networks.convnext import ConvNeXtAutoencoder
from src.neural_networks.simple_cnn import Autoencoder
from src.neural_networks.vae import VAE
from src.tasks import autoencoding
from src.training.callbacks import OptunaPruneCallback
from src.training.load_dataset import load_dataset
from src.training.loops import run_training, run_eval
import sys

# prevent tensorflow from loading everything onto the GPU, as we don't have enough memory for that
tf.config.experimental.set_visible_devices([], "GPU")

# initialize the pseudo-random number generator
seed = 0
rng = random.PRNGKey(seed=seed)

ae_type = "beta_vae"

latent_dim = 2
normalize_latent_space = True
max_num_epochs = 100
warmup_epochs = 5
batch_size = 100

now = datetime.now()
logdir = Path("logs") / "single_pendulum_autoencoding" / f"{now:%Y-%m-%d_%H-%M-%S}"
logdir.mkdir(parents=True, exist_ok=True)

if __name__ == "__main__":
    datasets, dataset_info, dataset_metadata = load_dataset(
        "mechanical_system/single_pendulum_64x64px",
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
        nn_model = VAE(latent_dim=latent_dim, img_shape=img_shape)
    else:
        nn_model = Autoencoder(latent_dim=latent_dim, img_shape=img_shape)

    # define the objective function for hyperparameter tuning
    def objective(trial):
        # Sample hyperparameters
        base_lr = trial.suggest_float("base_lr", 1e-5, 1e-2, log=True)
        beta = trial.suggest_float("beta", 1e-3, 1e1, log=True)
        b1 = 0.9
        b2 = 0.999
        weight_decay = trial.suggest_float("weight_decay", 1e-7, 1e-2, log=True)

        train_loss_weights = dict(mse_q=0.0, mse_rec=1.0, beta=beta)
        val_loss_weights = dict(mse_q=0.0, mse_rec=1.0, beta=1e0)

        if ae_type != "beta_vae":
            raise ValueError("Only beta_vae is supported for now")

        # call the factory function for the sensing task
        train_task_callables, train_metrics = autoencoding.task_factory(
            "pendulum",
            nn_model,
            loss_weights=train_loss_weights,
            normalize_latent_space=normalize_latent_space,
            # weight_on_foreground=0.15,
            ae_type=ae_type,
            eval=False,
        )

        prune_callback = OptunaPruneCallback(trial, metric_name="rmse_rec_val")
        print(f"Running trial {trial.number}...")
        (state, history, elapsed) = run_training(
            rng=rng,
            train_ds=train_ds,
            val_ds=val_ds,
            task_callables=train_task_callables,
            metrics=train_metrics,
            num_epochs=max_num_epochs,
            nn_model=nn_model,
            base_lr=base_lr,
            warmup_epochs=warmup_epochs,
            b1=b1,
            b2=b2,
            weight_decay=weight_decay,
            callbacks=[prune_callback],
            logdir=None,
            show_pbar=False,
        )

        val_loss_stps, val_rmse_rec_stps = history.collect("loss_val", "rmse_rec_val")
        print(
            f"Trial {trial.number} finished after {elapsed.steps} training steps with validation loss: {val_loss_stps[-1]:.5f}, rmse_rec: {val_rmse_rec_stps[-1]:.5f}"
        )

        return val_rmse_rec_stps[-1]

    # Add stream handler of stdout to show the messages
    optuna.logging.get_logger("optuna").addHandler(logging.StreamHandler(sys.stdout))
    study = optuna.create_study(
        pruner=optuna.pruners.SuccessiveHalvingPruner()
    )  # Create a new study.
    print("Run hyperparameter tuning...")
    study.optimize(
        objective, n_trials=100
    )  # Invoke optimization of the objective function.

    dill.dump(study, logdir / "optuna_study.dill")
