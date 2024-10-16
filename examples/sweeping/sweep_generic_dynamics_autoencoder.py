import os

# restrict to using one GPU
os.environ["CUDA_VISIBLE_DEVICES"] = "0"


from datetime import datetime
import dill
import flax.linen as nn
import jax

# jax.config.update("jax_platforms", "cpu")  # set default device to 'cpu'
jax.config.update("jax_enable_x64", True)
from jax import random
import jax.numpy as jnp
import numpy as onp
from pathlib import Path
import tensorflow as tf

from src.models.autoencoders import Autoencoder, VAE
from src.models.discrete_forward_dynamics import (
    DiscreteConIaeCfaDynamics,
    DiscreteCornn,
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

# set the system type in [
# "cc", "cs", "pcc_ns-2",
# "mass_spring_friction", "mass_spring_friction_actuation", "pendulum_friction", "double_pendulum_friction",
# "single_pendulum", "reaction_diffusion_default"]
system_type = "pcc_ns-2"
long_horizon_dataset = True
ae_type = "beta_vae"  # "None", "beta_vae", "wae"
""" dynamics_model_name in [
    "node-general-mlp", "node-mechanical-mlp", "node-mechanical-mlp-s",
    "node-cornn", "node-con", "node-w-con", "node-con-iae",  "node-con-iae-s", "node-lnn",
    "node-hippo-lss",
    "discrete-mlp", "discrete-elman-rnn", "discrete-gru-rnn", "discrete-general-lss", "discrete-hippo-lss", "discrete-mamba",
    "ar-con-iae-cfa", "ar-elman-rnn", "ar-gru-rnn", "ar-cornn"
]
"""
dynamics_model_name = "node-con-iae"
# simulation time step
if system_type in ["cc", "cs", "pcc_ns-2", "pcc_ns-3", "pcc_ns-4"]:
    sim_dt = 1e-2
elif system_type in [
    "single_pendulum",
    "double_pendulum",
    "mass_spring_friction",
    "mass_spring_friction_actuation",
    "pendulum_friction",
    "double_pendulum_friction",
    "reaction_diffusion_default",
]:
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

grayscale = True
match system_type:
    case "cs":
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # optimized for n_z=12
                base_lr = 0.00876101681360705
                loss_weights = dict(
                    mse_z=0.24637562277265898,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=87.35031665814813,
                    beta=0.0006062384910441915,
                )
                weight_decay = 3.9597147111138965e-05
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "softplus"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=12
                base_lr = 0.005698252456039503
                loss_weights = dict(
                    mse_z=0.185058657945758,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=24.66556644239504,
                    beta=0.0003754713308144261,
                )
                weight_decay = 8.491179551549292e-06
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
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
            case "ar-con-iae-cfa":
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
            case "ar-elman-rnn":
                # optimized for n_z=12
                base_lr = 0.010584414607144491
                loss_weights = dict(
                    mse_z=0.47946086114101605,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=30.23599724529981,
                    beta=0.0006044356658904357,
                )
                weight_decay = 2.8561405134373477e-05
            case "ar-gru-rnn":
                # optimized for n_z=12
                base_lr = 0.01659248975820467
                loss_weights = dict(
                    mse_z=0.4542033795488454,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.1003672840517102,
                    beta=0.00018196259604448875,
                )
                weight_decay = 8.527055579079863e-06
            case "ar-cornn":
                # optimized for n_z=12
                base_lr = 0.007011746009022523
                loss_weights = dict(
                    mse_z=0.11121798647531202,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=31.902035052144733,
                    beta=0.00014280447166145535,
                )
                weight_decay = 1.6883292308223353e-05
                cornn_gamma, cornn_epsilon = 0.01203854209212066, 0.12603002603875063
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "pcc_ns-2":
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # optimized for "node-general-mlp at n_z=8
                base_lr = 0.014939778657771675
                loss_weights = dict(
                    mse_z=0.11585323330519746,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.0855655639592068,
                    beta=0.00010190409372368565,
                )
                weight_decay = 6.3092347119914266e-6
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=8
                base_lr = 0.007137268676917664
                loss_weights = dict(
                    mse_z=0.17701201082200202,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=50.808302047597074,
                    beta=0.002678889167847793,
                )
                weight_decay = 4.5818408762378344e-05
                """
                originally tuned for
                num_mlp_layers, mlp_hidden_dim = 5, 21
                mlp_nonlinearity_name = "tanh"
                """
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
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
            case "ar-con-iae-cfa":
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
            case "ar-elman-rnn":
                # optimized for n_z=8
                base_lr = 0.007657437611794232
                loss_weights = dict(
                    mse_z=0.1842314509146704,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=81.49655648203793,
                    beta=0.00035525861444533717,
                )
                weight_decay = 1.7957485073520818e-05
            case "ar-gru-rnn":
                # optimized for n_z=8
                base_lr = 0.018086259222854423
                loss_weights = dict(
                    mse_z=0.4869102462993362,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=4.076717892106955,
                    beta=0.00015467929625107515,
                )
                weight_decay = 2.655293203579677e-05
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )

    case "pcc_ns-3":
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # Attention: not tuned yet
                base_lr = 0.014939778657771675
                loss_weights = dict(
                    mse_z=0.11585323330519746,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.0855655639592068,
                    beta=0.00010190409372368565,
                )
                weight_decay = 6.3092347119914266e-6
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=12
                base_lr = 0.005176340429875837
                loss_weights = dict(
                    mse_z=0.31615198615315904,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=16.051911333587626,
                    beta=0.00026524963767594775,
                )
                weight_decay = 1.7532877050933287e-05
                """
                originally tuned for
                num_mlp_layers, mlp_hidden_dim = 5, 21
                mlp_nonlinearity_name = "tanh"
                """
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=12
                base_lr = 0.013302787022802609
                loss_weights = dict(
                    mse_z=0.16836357054074094,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=14.471439878058165,
                    beta=0.0003269794498651636,
                    mse_tau_rec=5e1,
                )
                weight_decay = 1.220378359816519e-05
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-con-iae-cfa":
                # optimized for n_z=12
                base_lr = 0.014307900347859871
                loss_weights = dict(
                    mse_z=0.221692435490366,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=66.59826720119248,
                    beta=0.00018829492150535288,
                    mse_tau_rec=1e1,
                )
                weight_decay = 1.3789165265791588e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-elman-rnn":
                # optimized for n_z=12
                base_lr = 0.002017289539796043
                loss_weights = dict(
                    mse_z=0.23962047984932014,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=47.06787693448245,
                    beta=0.0001431412891866957,
                )
                weight_decay = 2.3556923826384874e-05
            case "ar-gru-rnn":
                # optimized for n_z=12
                base_lr = 0.010271074784281832
                loss_weights = dict(
                    mse_z=0.3302330270492981,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=91.12332977794878,
                    beta=0.0006241137175631403,
                )
                weight_decay = 2.6275213514548362e-05
            case "ar-cornn":
                # (quickly) optimized for n_z=12
                base_lr = 0.019118172838275722
                loss_weights = dict(
                    mse_z=0.17870826226404796,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=2.0483346931163813,
                    beta=0.00010138356977635644,
                )
                weight_decay = 1.9895341543311274e-05
                cornn_gamma, cornn_epsilon = 94.26788377109177, 12.930452341269934
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "mass_spring_friction":
        batch_size = 30
        num_epochs = 30
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # optimized for "node-general-mlp at n_z=4
                base_lr = 0.008339693008588403
                loss_weights = dict(
                    mse_z=0.24775689453393362,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.6087813703182756,
                    beta=0.00029500997565894957,
                )
                weight_decay = 1.540880150677294e-05
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=4
                base_lr = 0.006361540670994402
                loss_weights = dict(
                    mse_z=0.26781601891445145,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=17.134364197119655,
                    beta=0.0004806404295331634,
                )
                weight_decay = 4.3925638476059976e-05
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=4
                base_lr = 0.0056545951636144485
                loss_weights = dict(
                    mse_z=0.13320385919138733,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=85.20327288600144,
                    beta=0.0001986520795985124,
                    mse_tau_rec=5e1,
                )
                weight_decay = 7.226705864423811e-06
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-con-iae-cfa":
                # optimized for n_z=4
                base_lr = 0.008149533701551434
                loss_weights = dict(
                    mse_z=0.2441078460028391,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=53.342183614739334,
                    beta=0.00021457243252725872,
                    mse_tau_rec=1e1,
                )
                weight_decay = 4.642297296907424e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-elman-rnn":
                # optimized for n_z=4
                base_lr = 0.006923530586011298
                loss_weights = dict(
                    mse_z=0.20223686992178358,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=60.74996073425695,
                    beta=0.0002417917724332947,
                )
                weight_decay = 4.59867549628406e-05
            case "ar-gru-rnn":
                # optimized for n_z=4
                base_lr = 0.019842372307125208
                loss_weights = dict(
                    mse_z=0.16620611214172962,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=3.1512643907782274,
                    beta=0.00023992946888229258,
                )
                weight_decay = 2.0100052551831417e-05
            case "ar-cornn":
                # optimized for n_z=4
                base_lr = 0.013708056733757825
                loss_weights = dict(
                    mse_z=0.10764849491271784,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=41.87535961465608,
                    beta=0.00011207010422006182,
                )
                weight_decay = 5.5064434667621325e-06
                cornn_gamma, cornn_epsilon = 1.237065995901295, 0.02054193941627336
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "mass_spring_friction_actuation":
        batch_size = 30
        num_epochs = 30
        match dynamics_model_name:
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=1
                base_lr = 0.010691140531761378
                loss_weights = dict(
                    mse_z=0.483373283813048,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=3.7216197739429715,
                    beta=0.0001946301846127814,
                    mse_tau_rec=5e1,
                )
                weight_decay = 1.070113135475619e-05
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=1
                base_lr = 0.010904629374008561
                loss_weights = dict(
                    mse_z=0.1037876096372912,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=3.011464307772081,
                    beta=0.0009937029783177562,
                )
                weight_decay = 7.641634176772359e-06
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "pendulum_friction":
        batch_size = 30
        num_epochs = 150
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # optimized for "node-general-mlp at n_z=4
                base_lr = 0.0029404842973983734
                loss_weights = dict(
                    mse_z=0.11846459037217552,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=82.41447723083671,
                    beta=0.0007865401169172522,
                )
                weight_decay = 1.3251268676344262e-05
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=4
                base_lr = 0.00904783438825653
                loss_weights = dict(
                    mse_z=0.1296496100573024,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=76.4403216760019,
                    beta=0.0006688707408161218,
                )
                weight_decay = 1.5792346139343144e-05
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=4
                base_lr = 0.011064147131784937
                loss_weights = dict(
                    mse_z=0.32103670208740176,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=96.75643936202472,
                    beta=0.0003362481655813636,
                    mse_tau_rec=5e1,
                )
                weight_decay = 7.422752602130393e-06
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-con-iae-cfa":
                # optimized for n_z=4
                base_lr = 0.010754656092162381
                loss_weights = dict(
                    mse_z=0.2678697703682045,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=94.69625942730633,
                    beta=0.00058540004856265,
                    mse_tau_rec=1e1,
                )
                weight_decay = 2.271612879994102e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-elman-rnn":
                # optimized for n_z=4
                base_lr = 0.0018763341236812678
                loss_weights = dict(
                    mse_z=0.11008600010756936,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=63.41034369539617,
                    beta=0.00010155994014321249,
                )
                weight_decay = 1.5545057591638582e-05
            case "ar-gru-rnn":
                # optimized for n_z=4
                base_lr = 0.018759092183177933
                loss_weights = dict(
                    mse_z=0.36189940167672224,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=8.37449686843681,
                    beta=0.00013130293256147146,
                )
                weight_decay = 3.016471182021465e-05
            case "ar-cornn":
                # optimized for n_z=4
                base_lr = 0.009092269329911869
                loss_weights = dict(
                    mse_z=0.23777524637531147,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=53.59893896721093,
                    beta=0.00040966721384268976,
                )
                weight_decay = 5.169062143268791e-06
                cornn_gamma, cornn_epsilon = 0.49806996377185103, 0.6971087305725828
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "double_pendulum_friction":
        batch_size = 10
        num_epochs = 150
        grayscale = False
        match dynamics_model_name:
            case "node-general-mlp" | "node-general-mlp-s":
                # optimized for "node-general-mlp at n_z=12
                base_lr = 0.003789514088335835
                loss_weights = dict(
                    mse_z=0.10354674975294657,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=37.43925221480726,
                    beta=0.0004462734231619719,
                )
                weight_decay = 8.042584606568567e-06
                if dynamics_model_name == "node-general-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-mechanical-mlp" | "node-mechanical-mlp-s":
                # optimized for n_z=12
                base_lr = 0.006923530586011298
                loss_weights = dict(
                    mse_z=0.20223686992178358,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=60.74996073425695,
                    beta=0.0002417917724332947,
                )
                weight_decay = 4.59867549628406e-05
                if dynamics_model_name == "node-mechanical-mlp-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-con-iae" | "node-con-iae-s":
                # optimized for n_z=12
                base_lr = 0.004371111690634623
                loss_weights = dict(
                    mse_z=0.10002120482256567,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=51.70644278103679,
                    beta=0.00027963911733805484,
                    mse_tau_rec=5e1,
                )
                weight_decay = 2.1177073207071563e-05
                if dynamics_model_name == "node-con-iae-s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-con-iae-cfa":
                # optimized for n_z=12
                base_lr = 0.0072470536816657384
                loss_weights = dict(
                    mse_z=0.11961092952593712,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=70.78205762849846,
                    beta=0.0002645522976826533,
                    mse_tau_rec=1e1,
                )
                weight_decay = 1.609243419193349e-05
                num_mlp_layers, mlp_hidden_dim = 5, 30
            case "ar-elman-rnn":
                # optimized for n_z=12
                base_lr = 0.004524443032980358
                loss_weights = dict(
                    mse_z=0.12027467845758552,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.4056183787726764,
                    beta=0.0007909511260972457,
                )
                weight_decay = 4.617738677622606e-05
            case "ar-gru-rnn":
                # optimized for n_z=12
                base_lr = 0.018759092183177933
                loss_weights = dict(
                    mse_z=0.36189940167672224,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=8.37449686843681,
                    beta=0.00013130293256147146,
                )
                weight_decay = 3.016471182021465e-05
            case "ar-cornn":
                # optimized for n_z=12
                base_lr = 0.010072831724792584
                loss_weights = dict(
                    mse_z=0.10054968603804583,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=8.295226425347138,
                    beta=0.00018624281835822343,
                )
                weight_decay = 1.3424702241051468e-05
                cornn_gamma, cornn_epsilon = 1.8202725088035752, 0.12389358153779872
            case _:
                raise NotImplementedError(
                    f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
                )
    case "reaction_diffusion_default":
        batch_size = 10
        grayscale = False
        match dynamics_model_name:
            case (
                "node-general-mlp"
                | "node-general-mlp-s"
                | "node-mechanical-mlp"
                | "node-mechanical-mlp-s"
            ):
                # optimized for "node-general-mlp at n_z=4
                base_lr = 0.0059168868877279915
                loss_weights = dict(
                    mse_z=0.10889131669317606,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.1653845909300706,
                    beta=0.0008282714338681616,
                )
                weight_decay = 1.2611460334026154e-05
                if dynamics_model_name.split("-")[-1] == "s":
                    num_mlp_layers, mlp_hidden_dim = 2, 12
                else:
                    num_mlp_layers, mlp_hidden_dim = 5, 30
                mlp_nonlinearity_name = "tanh"
            case "node-con-iae" | "node-con-iae-s":
                # # optimized for n_z = 2
                # base_lr = 0.005888917625351585
                # loss_weights = dict(
                #     mse_z=0.13054403694912117,
                #     mse_rec_static=1.0,
                #     mse_rec_dynamic=2.6387201915885408,
                #     beta=0.0006981872905554007,
                #     mse_tau_rec=0e0,
                # )
                # weight_decay = 1.1276927331777516e-05
                # optimized for n_z = 4
                base_lr = 0.012801121754750396
                loss_weights = dict(
                    mse_z=0.10501370568062449,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=48.19382647228793,
                    beta=0.00022646053215673954,
                    mse_tau_rec=0e0,
                )
                weight_decay = 1.163668844018335e-05
                # the dataset doesn't consider inputs
                num_mlp_layers, mlp_hidden_dim = 0, 0
            case "ar-con-iae-cfa":
                # optimized for n_z=4
                base_lr = 0.009766329444231621
                loss_weights = dict(
                    mse_z=0.11552971801128281,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=15.563769911124428,
                    beta=0.0003136141073989516,
                    mse_tau_rec=0e0,
                )
                weight_decay = 4.096005404399632e-05
                num_mlp_layers, mlp_hidden_dim = 0, 0
            case "ar-elman-rnn":
                # optimized for n_z=4
                base_lr = 0.0019378423391292106
                loss_weights = dict(
                    mse_z=0.31809709978762846,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=9.093909927255865,
                    beta=0.0003311525313441982,
                )
                weight_decay = 3.5656743768711664e-05
            case "ar-gru-rnn":
                # optimized for n_z=4
                base_lr = 0.013572443532482735
                loss_weights = dict(
                    mse_z=0.2867771990437324,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=45.04050103443851,
                    beta=0.0008503337887834961,
                )
                weight_decay = 4.5713276028459896e-05
            case "ar-cornn":
                # optimized for n_z=4
                base_lr = 0.011772031105770487
                loss_weights = dict(
                    mse_z=0.28431186155509935,
                    mse_rec_static=1.0,
                    mse_rec_dynamic=1.0137752787206968,
                    beta=0.0004800600944551659,
                )
                weight_decay = 1.9751997327387143e-05
                cornn_gamma, cornn_epsilon = 1.165320622900866, 5.731626408645574
    case _:
        raise NotImplementedError(
            f"{system_type} with dynamics_model_name '{dynamics_model_name}' not implemented yet."
        )

# identify the dynamics_type
dynamics_type = dynamics_model_name.split("-")[0]
assert dynamics_type in [
    "node",
    "discrete",
    "ar",
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

            dynamics_order = 2
            if system_type in ["cc", "cs", "pcc_ns-2", "pcc_ns-3", "pcc_ns-4"]:
                dataset_type = "planar_pcs"
            elif system_type in ["single_pendulum", "double_pendulum"]:
                dataset_type = "pendulum"
            elif system_type == "reaction_diffusion_default":
                dataset_type = "reaction_diffusion"
                grayscale = False
                dynamics_order = 1
            elif system_type in [
                "mass_spring_friction",
                "mass_spring_friction_actuation",
                "pendulum_friction",
                "double_pendulum_friction",
            ]:
                dataset_type = "toy_physics"
            else:
                raise ValueError(f"Unknown system_type: {system_type}")

            dataset_name_postfix = ""
            if not dataset_type in ["reaction_diffusion"]:
                if dataset_type == "toy_physics":
                    dataset_name_postfix += f"_dt_0_05"
                else:
                    dataset_name_postfix += f"_32x32px"
                if dataset_type != "toy_physics":
                    dataset_name_postfix += f"_h-101"

            dataset_name = f"{dataset_type}/{system_type}{dataset_name_postfix}"
            if dataset_type == "toy_physics":
                load_dataset_type = "dm_hamiltonian_dynamics_suite"
            elif dataset_type == "reaction_diffusion":
                load_dataset_type = "reaction_diffusion"
            else:
                load_dataset_type = "jsrm"
            datasets, dataset_info, dataset_metadata = load_dataset(
                dataset_name,
                seed=seed,
                batch_size=batch_size,
                num_epochs=num_epochs,
                normalize=True,
                grayscale=grayscale,
                dataset_type=load_dataset_type,
            )
            train_ds, val_ds, test_ds = (
                datasets["train"],
                datasets["val"],
                datasets["test"],
            )

            # extract the robot parameters from the dataset
            robot_params = dataset_metadata.get("system_params", {})
            # size of torques
            n_tau = train_ds.element_spec["tau"].shape[
                -1
            ]  # dimension of the control input
            if system_type in ["reaction_diffusion_default"]:
                n_tau = 0
            print(f"n_tau: {n_tau}")
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
            state_dim = n_z if dynamics_order == 1 else 2 * n_z
            if dynamics_model_name in [
                "node-general-mlp",
                "node-mechanical-mlp",
                "node-mechanical-mlp-s",
            ]:
                dynamics_model = MlpOde(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    dynamics_order=dynamics_order,
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
                    dynamics_order=dynamics_order,
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
                    dynamics_order=dynamics_order,
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
            elif dynamics_model_name == "ar-con-iae-cfa":
                dynamics_model = DiscreteConIaeCfaDynamics(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    dt=sim_dt,
                    dynamics_order=dynamics_order,
                    num_layers=num_mlp_layers,
                    hidden_dim=mlp_hidden_dim,
                )
            elif dynamics_model_name in ["ar-elman-rnn", "ar-gru-rnn"]:
                dynamics_model = DiscreteRnnDynamics(
                    state_dim=state_dim,
                    input_dim=n_tau,
                    output_dim=state_dim,
                    rnn_method=dynamics_model_name.split("-")[1],  # "elman" or "gru"
                )
            elif dynamics_model_name == "ar-cornn":
                dynamics_model = DiscreteCornn(
                    latent_dim=n_z,
                    input_dim=n_tau,
                    dynamics_order=dynamics_order,
                    dt=sim_dt,
                    gamma=cornn_gamma,
                    epsilon=cornn_epsilon,
                )
            else:
                raise ValueError(f"Unknown dynamics_model_name: {dynamics_model_name}")
            nn_model = DynamicsAutoencoder(
                autoencoder=autoencoder_model,
                dynamics=dynamics_model,
                dynamics_type=dynamics_type,
                dynamics_order=dynamics_order,
                num_past_timesteps=num_past_timesteps,
            )

            solver_class_name = dataset_metadata.get("solver_class", "Dopri5")
            # import solver class from diffrax
            # https://stackoverflow.com/questions/6677424/how-do-i-import-variable-packages-in-python-like-using-variable-variables-i
            solver_class = getattr(
                __import__("diffrax", fromlist=[solver_class_name]),
                solver_class_name,
            )

            # call the factory function for the dynamics autoencoder task
            task_callables_train, metrics_collection_cls_train = (
                dynamics_autoencoder.task_factory(
                    system_type,
                    nn_model,
                    ts=dataset_metadata["ts"],
                    sim_dt=sim_dt,
                    loss_weights=loss_weights,
                    ae_type=ae_type,
                    dynamics_type=dynamics_type,
                    dynamics_order=dynamics_order,
                    start_time_idx=start_time_idx,
                    solver=solver_class(),
                    latent_velocity_source=latent_velocity_source,
                    num_past_timesteps=num_past_timesteps,
                )
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
            task_callables_test, metrics_collection_cls_test = (
                dynamics_autoencoder.task_factory(
                    system_type,
                    nn_model,
                    ts=dataset_metadata["ts"],
                    sim_dt=sim_dt,
                    loss_weights=loss_weights,
                    ae_type=ae_type,
                    dynamics_type=dynamics_type,
                    dynamics_order=dynamics_order,
                    start_time_idx=start_time_idx,
                    solver=solver_class(),
                    latent_velocity_source=latent_velocity_source,
                    num_past_timesteps=num_past_timesteps,
                    compute_psnr=True,
                    compute_ssim=True,
                )
            )

            # load the neural network dummy input
            nn_dummy_input = load_dummy_neural_network_input(
                test_ds, task_callables_test
            )
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
