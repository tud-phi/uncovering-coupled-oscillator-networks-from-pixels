import os
# restrict to using one GPU
os.environ['CUDA_VISIBLE_DEVICES'] = '0'


from datetime import datetime
import dill
import flax.linen as nn
import jax

# jax.config.update("jax_platforms", "cpu")  # set default device to 'cpu'
jax.config.update("jax_enable_x64", True)
from jax import random
import jax.numpy as jnp
import jsrm
import numpy as onp
from pathlib import Path
import tensorflow as tf

from src.models.autoencoders import Autoencoder, VAE
from src.models.discrete_forward_dynamics import (
    DiscreteConIaeCfaDynamics,
    DiscreteLssDynamics,
    DiscreteMambaDynamics,
    DiscreteMlpDynamics,
    DiscreteRnnDynamics,
)
from src.models.neural_odes import (
    ConOde,
    ConIaeOde,
    CornnOde,
    LnnOde,
    LinearStateSpaceOde,
    MambaOde,
    MlpOde,
)
from src.models.dynamics_autoencoder import DynamicsAutoencoder
from src.tasks import dynamics_autoencoder
from src.training.dataset_utils import load_dataset, load_dummy_neural_network_input
from src.training.loops import run_eval, run_training
from src.training.train_state_utils import (
    count_number_of_trainable_params,
    restore_train_state,
)


def concat_or_none(x, y, **kwargs):
    if x is None:
        return y
    return onp.concatenate([x, y], **kwargs)


# prevent tensorflow from loading everything onto the GPU, as we don't have enough memory for that
tf.config.experimental.set_visible_devices([], "GPU")

# set sweep settings
# set the range of latent dimensions
n_z_range = onp.arange(1, 32, step=2)
# set the range of random seeds
seed_range = onp.array([0, 1, 2])

# set the system type in ["single_pendulum", "cc", "cs", "pcc_ns-2"]
system_type = "pcc_ns-2"
long_horizon_dataset = True
ae_type = "beta_vae"  # "None", "beta_vae", "wae"
""" dynamics_model_name in [
    "node-general-mlp", "node-mechanical-mlp", "node-mechanical-mlp-s", 
    "node-cornn", "node-con", "node-w-con", "node-con-iae",  "node-con-iae-s", "node-dcon", "node-lnn", 
    "node-hippo-lss", "node-mamba",
    "discrete-mlp", "discrete-elman-rnn", "discrete-gru-rnn", "discrete-general-lss", "discrete-hippo-lss", "discrete-mamba",
    "dsim-con-iae-cfa"
]
"""
dynamics_model_name = "node-con-iae-s"
# simulation time step
if system_type in ["cc", "cs",  "pcc_ns-2", "pcc_ns-3", "pcc_ns-4"]:
    sim_dt = 1e-2
elif system_type in ["single_pendulum", "double_pendulum"]:
    sim_dt = 2.5e-2
else:
    raise ValueError(f"Unknown system_type: {system_type}")

batch_size = 80
num_epochs = 50
warmup_epochs = 5
start_time_idx = 1
num_past_timesteps = 2

latent_velocity_source = "image-space-finite-differences"
num_mlp_layers, mlp_hidden_dim, mlp_nonlinearity_name = 4, 20, "leaky_relu"
cornn_gamma, cornn_epsilon = 1.0, 1.0
lnn_learn_dissipation = True
diag_shift, diag_eps = 1e-6, 2e-6


assert long_horizon_dataset, "Only long horizon datasets are supported."
assert ae_type == "beta_vae", "Only beta_vae is supported."

match system_type:
    case "cs":
        match dynamics_model_name:
            case "node-general-mlp":
                # optimized for n_z=12
                base_lr = 0.00876101681360705
                loss_weights = dict(
                    mse_z=0.24637562277265898,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=87.35031665814813,
                    beta=0.0006062384910441915,
                )
                weight_decay = 3.9597147111138965e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "softplus"
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=12
                base_lr = 0.0132475538170814
                loss_weights = dict(
                    mse_z=0.20406939884429706,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=60.421163619903474,
                    beta=0.00019690018264350686,
                    mse_tau_rec=1e1,
                )
                weight_decay = 2.215820759288193e-05
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "dsim-con-iae-cfa":
                # optimized for n_z=12
                base_lr = 0.01082596684679984
                loss_weights = dict(
                    mse_z=0.19775278538920418,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=75.17041375999705,
                    beta=0.00019171408190837293,
                    mse_tau_rec=1e1,
                )
                weight_decay = 4.957164364541807e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "pcc_ns-2":
        match dynamics_model_name:
            case "node-mechanical-mlp":
                # optimized for n_z=8
                base_lr = 0.007137268676917664
                loss_weights = dict(
                    mse_z=0.17701201082200202,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=50.808302047597074,
                    beta=0.002678889167847793,
                )
                weight_decay = 4.5818408762378344e-05
                num_mlp_layers, mlp_hidden_dim = 5, 21
                mlp_nonlinearity_name = "tanh"
            case "node-w-con":
                # optimized for n_z=32
                base_lr = 0.009793849772267547
                loss_weights = dict(
                    mse_z=0.40568126085978073,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=64.68788840647458,
                    beta=0.0002437097576124702,
                )
                weight_decay = 1.3691415073322272e-05
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=8
                base_lr = 0.018486990918444367
                loss_weights = dict(
                    mse_z=0.3733687489479885,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=83.7248326772002,
                    beta=0.00020068384639167935,
                    mse_tau_rec=5e1,
                )
                weight_decay = 5.5340117045438595e-06
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "dsim-con-iae-cfa":
                # optimized for n_z=8
                base_lr = 0.018088317332901616
                loss_weights = dict(
                    mse_z=0.10824911140537369,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=80.45564515992584,
                    beta=0.00010659152931072577,
                    mse_tau_rec=1e1,
                )
                weight_decay = 2.6404635847920316e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )

# identify the number of segments
if system_type in ["single_pendulum", "cs", "cc"]:
    num_segments = 1
elif system_type.split("_")[0] == "pcc":
    num_segments = int(system_type.split("-")[-1])
else:
    raise ValueError(f"Unknown system_type: {system_type}")
print(f"Number of segments: {num_segments}")

# identify the dynamics_type
dynamics_type = dynamics_model_name.split("-")[0]
assert dynamics_type in [
    "node",
    "discrete",
    "dsim",
], f"Unknown dynamics_type: {dynamics_type}"

now = datetime.now()
logdir = (
    Path("logs").resolve()
    / f"{system_type}_dynamics_autoencoder"
    / f"{now:%Y-%m-%d_%H-%M-%S}"
)
logdir.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    # initialize dictionary with experimental results
    zero_array = None
    sweep_results = dict(
        n_z=zero_array,
        seed=zero_array,
        num_trainable_params=dict(
            total=zero_array,
            autoencoder=zero_array,
            dynamics=zero_array,
        ),
        train=dict(
            rmse_rec_static=zero_array,
            rmse_rec_dynamic=zero_array,
            psnr_rec_static=zero_array,
            psnr_rec_dynamic=zero_array,
            ssim_rec_static=zero_array,
            ssim_rec_dynamic=zero_array,
        ),
        test=dict(
            rmse_rec_static=zero_array,
            rmse_rec_dynamic=zero_array,
            psnr_rec_static=zero_array,
            psnr_rec_dynamic=zero_array,
            ssim_rec_static=zero_array,
            ssim_rec_dynamic=zero_array,
        ),
    )
    for n_z in n_z_range:
        for seed in seed_range:
            # initialize the pseudo-random number generator
            rng = random.PRNGKey(seed=seed)
            tf.random.set_seed(seed=seed)

            # specify the folder
            logdir_run = logdir / f"n_z_{n_z}_seed_{seed}"

            if system_type in ["cc", "cs", "pcc_ns-2", "pcc_ns-3", "pcc_ns-4"]:
                dataset_type = "planar_pcs"
            elif system_type in ["single_pendulum", "double_pendulum"]:
                dataset_type = "pendulum"
            else:
                raise ValueError(f"Unknown system_type: {system_type}")

            dataset_name_postfix = ""
            if long_horizon_dataset:
                dataset_name_postfix = f"_h-101"

            dataset_name = f"{dataset_type}/{system_type}_32x32px{dataset_name_postfix}"
            datasets, dataset_info, dataset_metadata = load_dataset(
                dataset_name,
                seed=seed,
                batch_size=batch_size,
                normalize=True,
                grayscale=True,
            )
            train_ds, val_ds, test_ds = (
                datasets["train"],
                datasets["val"],
                datasets["test"],
            )

            # extract the robot parameters from the dataset
            robot_params = dataset_metadata["system_params"]
            # size of torques
            n_tau = train_ds.element_spec["tau"].shape[
                -1
            ]  # dimension of the control input=
            # image shape
            img_shape = train_ds.element_spec["rendering_ts"].shape[-3:]  # image shape

            # initialize the neural networks
            if ae_type == "beta_vae":
                autoencoder_model = VAE(
                    latent_dim=n_z, img_shape=img_shape, norm_layer=nn.LayerNorm
                )
            else:
                autoencoder_model = Autoencoder(
                    latent_dim=n_z, img_shape=img_shape, norm_layer=nn.LayerNorm
                )
            if dynamics_model_name in [
                "node-general-mlp",
                "node-mechanical-mlp",
                "node-mechanical-mlp-s",
            ]:
                dynamics_model = MlpOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                    nonlinearity=getattr(nn, mlp_nonlinearity_name),
                    mechanical_system=True
                    if dynamics_model_name.split("-")[1] == "mechanical"
                    else False,
                )
            elif dynamics_model_name == "node-cornn":
                dynamics_model = CornnOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    gamma=cornn_gamma,
                    epsilon=cornn_epsilon,
                )
            elif dynamics_model_name in ["node-con", "node-w-con"]:
                dynamics_model = ConOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    use_w_coordinates=dynamics_model_name == "node-w-con",
                )
            elif dynamics_model_name in ["node-con-iae", "node-con-iae-s"]:
                dynamics_model = ConIaeOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                )
            elif dynamics_model_name == "node-lnn":
                dynamics_model = LnnOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    learn_dissipation=lnn_learn_dissipation,
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                    nonlinearity=getattr(nn, mlp_nonlinearity_name),
                    diag_shift=diag_shift,
                    diag_eps=diag_eps,
                )
            elif dynamics_model_name in [
                "node-general-lss",
                "node-mechanical-lss",
                "node-hippo-lss",
            ]:
                dynamics_model = LinearStateSpaceOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    transition_matrix_init=dynamics_model_name.split("-")[
                        1
                    ],  # "general", "mechanical", or "hippo"
                )
            elif dynamics_model_name == "discrete-mlp":
                dynamics_model = DiscreteMlpDynamics(
                    state_dim=num_past_timesteps * n_z,
                    input_dim=num_past_timesteps * n_tau,
                    output_dim=n_z,
                    dt=dataset_metadata["dt"],
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                    nonlinearity=getattr(nn, mlp_nonlinearity_name),
                )
            elif dynamics_model_name in ["discrete-elman-rnn", "discrete-gru-rnn"]:
                dynamics_model = DiscreteRnnDynamics(
                    state_dim=num_past_timesteps * n_z,
                    input_dim=num_past_timesteps * n_tau,
                    output_dim=n_z,
                    rnn_method=dynamics_model_name.split("-")[1],  # "elman" or "gru"
                )
            elif dynamics_model_name == "discrete-mamba":
                dynamics_model = DiscreteMambaDynamics(
                    state_dim=num_past_timesteps * n_z,
                    input_dim=num_past_timesteps * n_tau,
                    output_dim=n_z,
                    dt=dataset_metadata["dt"],
                )
            elif dynamics_model_name == "dsim-con-iae-cfa":
                dynamics_model = DiscreteConIaeCfaDynamics(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    dt=sim_dt,
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                )
            else:
                raise ValueError(f"Unknown dynamics_model_name: {dynamics_model_name}")
            nn_model = DynamicsAutoencoder(
                autoencoder=autoencoder_model,
                dynamics=dynamics_model,
                dynamics_type=dynamics_type,
                num_past_timesteps=num_past_timesteps,
            )

            # import solver class from diffrax
            # https://stackoverflow.com/questions/6677424/how-do-i-import-variable-packages-in-python-like-using-variable-variables-i
            solver_class = getattr(
                __import__("diffrax", fromlist=[dataset_metadata["solver_class"]]),
                dataset_metadata["solver_class"],
            )

            # call the factory function for the dynamics autoencoder task
            task_callables_train, metrics_collection_cls_train = dynamics_autoencoder.task_factory(
                system_type,
                nn_model,
                ts=dataset_metadata["ts"],
                sim_dt=sim_dt,
                loss_weights=loss_weights,
                ae_type=ae_type,
                dynamics_type=dynamics_type,
                start_time_idx=start_time_idx,
                solver=solver_class(),
                latent_velocity_source=latent_velocity_source,
                num_past_timesteps=num_past_timesteps,
            )

            # run the training loop
            print(f"Run training for n_z={n_z}, seed={seed}...")
            (state, train_history, elapsed) = run_training(
                rng=rng,
                train_ds=train_ds,
                val_ds=val_ds,
                task_callables=task_callables_train,
                metrics_collection_cls=metrics_collection_cls_train,
                num_epochs=num_epochs,
                nn_model=nn_model,
                init_fn=nn_model.forward_all_layers,
                base_lr=base_lr,
                warmup_epochs=warmup_epochs,
                weight_decay=weight_decay,
                logdir=logdir_run,
            )
            train_metrics = state.metrics.compute()
            print(
                f"Final training metrics for n_z={n_z}, seed={seed}:\n", train_metrics
            )

            # count the number of trainable parameters
            params_count = count_number_of_trainable_params(state, verbose=False)

            # call the factory function for the dynamics autoencoder task
            task_callables_test, metrics_collection_cls_test = dynamics_autoencoder.task_factory(
                system_type,
                nn_model,
                ts=dataset_metadata["ts"],
                sim_dt=sim_dt,
                loss_weights=loss_weights,
                ae_type=ae_type,
                dynamics_type=dynamics_type,
                start_time_idx=start_time_idx,
                solver=solver_class(),
                latent_velocity_source=latent_velocity_source,
                num_past_timesteps=num_past_timesteps,
                compute_psnr=True,
                compute_ssim=True,
            )

            # load the neural network dummy input
            nn_dummy_input = load_dummy_neural_network_input(test_ds, task_callables_test)
            # load the training state from the checkpoint directory
            state = restore_train_state(
                rng=rng,
                ckpt_dir=logdir_run,
                nn_model=nn_model,
                nn_dummy_input=nn_dummy_input,
                metrics_collection_cls=metrics_collection_cls_test,
                init_fn=nn_model.forward_all_layers,
            )

            print(f"Run testing for n_z={n_z}, seed={seed}...")
            state, test_history = run_eval(test_ds, state, task_callables_test)
            test_metrics = state.metrics.compute()
            print(
                "\n"
                f"Final test metrics for n_z={n_z}, seed={seed}:\n"
                f"rmse_rec_static={test_metrics['rmse_rec_static']:.4f}, "
                f"rmse_rec_dynamic={test_metrics['rmse_rec_dynamic']:.4f}, "
                f"psnr_rec_static={test_metrics['psnr_rec_static']:.4f}, "
                f"psnr_rec_dynamic={test_metrics['psnr_rec_dynamic']:.4f}, "
                f"ssim_rec_static={test_metrics['ssim_rec_static']:.4f}, "
                f"ssim_rec_dynamic={test_metrics['ssim_rec_dynamic']:.4f}"
            )

            # update sweep results
            sweep_results["n_z"] = concat_or_none(
                sweep_results["n_z"], onp.array(n_z)[None, ...], axis=0
            )
            sweep_results["seed"] = concat_or_none(
                sweep_results["seed"], onp.array(seed)[None, ...], axis=0
            )
            sweep_results["num_trainable_params"]["total"] = concat_or_none(
                sweep_results["num_trainable_params"]["total"],
                onp.array(params_count["total"])[None, ...],
                axis=0,
            )
            sweep_results["num_trainable_params"]["autoencoder"] = concat_or_none(
                sweep_results["num_trainable_params"]["autoencoder"],
                onp.array(params_count["autoencoder"])[None, ...],
                axis=0,
            )
            sweep_results["num_trainable_params"]["dynamics"] = concat_or_none(
                sweep_results["num_trainable_params"]["dynamics"],
                onp.array(params_count["dynamics"])[None, ...],
                axis=0,
            )
            sweep_results["train"]["rmse_rec_static"] = concat_or_none(
                sweep_results["train"]["rmse_rec_static"],
                train_metrics["rmse_rec_static"][None, ...],
                axis=0,
            )
            sweep_results["train"]["rmse_rec_dynamic"] = concat_or_none(
                sweep_results["train"]["rmse_rec_dynamic"],
                train_metrics["rmse_rec_dynamic"][None, ...],
                axis=0,
            )
            sweep_results["test"]["rmse_rec_static"] = concat_or_none(
                sweep_results["test"]["rmse_rec_static"],
                test_metrics["rmse_rec_static"][None, ...],
                axis=0,
            )
            sweep_results["test"]["rmse_rec_dynamic"] = concat_or_none(
                sweep_results["test"]["rmse_rec_dynamic"],
                test_metrics["rmse_rec_dynamic"][None, ...],
                axis=0,
            )
            sweep_results["test"]["psnr_rec_static"] = concat_or_none(
                sweep_results["test"]["psnr_rec_static"],
                test_metrics["psnr_rec_static"][None, ...],
                axis=0,
            )
            sweep_results["test"]["psnr_rec_dynamic"] = concat_or_none(
                sweep_results["test"]["psnr_rec_dynamic"],
                test_metrics["psnr_rec_dynamic"][None, ...],
                axis=0,
            )
            sweep_results["test"]["ssim_rec_static"] = concat_or_none(
                sweep_results["test"]["ssim_rec_static"],
                test_metrics["ssim_rec_static"][None, ...],
                axis=0,
            )
            sweep_results["test"]["ssim_rec_dynamic"] = concat_or_none(
                sweep_results["test"]["ssim_rec_dynamic"],
                test_metrics["ssim_rec_dynamic"][None, ...],
                axis=0,
            )

            # save the experimental results
            with open(logdir / "sweep_results.dill", "wb") as f:
                dill.dump(sweep_results, f)
