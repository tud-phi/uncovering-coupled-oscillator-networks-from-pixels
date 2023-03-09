from jax import random

from src.neural_networks.simple_cnn import Encoder
from src.training.tasks import sensing
from src.training.initialization import initialize_train_state
from src.training.load_dataset import load_dataset
from src.training.optim import create_learning_rate_fn

# initialize the pseudo-random number generator
rng = random.PRNGKey(seed=0)

num_epochs = 1
batch_size = 32

if __name__ == "__main__":
    datasets = load_dataset(
        "mechanical_system/single_pendulum",
        batch_size=batch_size,
        normalize=True,
        grayscale=True,
    )
    train_ds, val_ds, test_ds = datasets["train"], datasets["val"], datasets["test"]
    print("train_ds: ", train_ds)
    print("val_ds: ", val_ds)
    print("test_ds: ", test_ds)

    num_steps_per_epoch = train_ds.cardinality().numpy()
    print("num_steps_per_epoch: ", num_steps_per_epoch)

    # create learning rate schedule
    lr_fn = create_learning_rate_fn(
        num_epochs,
        steps_per_epoch=num_steps_per_epoch,
        base_lr=1e-4,
        warmup_epochs=0,
    )

    # initialize the model
    nn_model = Encoder(latent_dim=2)

    train_state = None
    for step, batch in enumerate(datasets["train"].as_numpy_iterator()):
        print("step: ", step)
        if step == 0:
            nn_dummy_input = sensing.preprocess_batch(batch)

            # initialize the train state
            train_state = initialize_train_state(
                rng,
                nn_model,
                nn_dummy_input=nn_dummy_input,
                learning_rate_fn=lr_fn
            )

        sensing.model_forward(nn_model, train_state.params, batch)
