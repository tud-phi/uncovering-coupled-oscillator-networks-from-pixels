from flax import linen as nn  # Linen API
import jax.numpy as jnp
import math
from typing import Callable, Sequence, Tuple


class Encoder(nn.Module):
    """A simple CNN encoder."""

    latent_dim: int
    strides: Tuple[int, int] = (1, 1)
    nonlinearity: Callable = nn.leaky_relu

    @nn.compact
    def __call__(self, x):
        x = nn.Conv(features=16, kernel_size=(3, 3), strides=self.strides)(x)
        x = self.nonlinearity(x)
        x = nn.Conv(features=32, kernel_size=(3, 3), strides=self.strides)(x)
        x = self.nonlinearity(x)
        x = x.reshape((x.shape[0], -1))  # flatten

        x = nn.Dense(features=256)(x)
        x = self.nonlinearity(x)
        x = nn.Dense(features=self.latent_dim)(x)

        # clip to [-1, 1]
        # doesn't seem to work.
        # x = -1.0 + 2 * nn.softmax(x)

        return x


class Decoder(nn.Module):
    """A simple CNN decoder."""

    downsampled_img_dim: Sequence = (2, 2, 768)
    strides: Tuple[int, int] = (1, 1)
    nonlinearity: Callable = nn.leaky_relu

    @nn.compact
    def __call__(self, x):
        x = self.nonlinearity(x)
        x = nn.Dense(features=256)(x)
        x = self.nonlinearity(x)

        x = nn.Dense(features=math.prod(self.downsampled_img_dim))(x)
        x = x.reshape((
                x.shape[0],  # batch size
                *self.downsampled_img_dim
            )
        )  # unflatten

        x = nn.Conv(features=16, kernel_size=(3, 3), strides=self.strides)(x)
        x = self.nonlinearity(x)
        x = nn.Conv(features=self.img_shape[-1], kernel_size=(3, 3), strides=self.strides)(x)

        # clip to [-1, 1]
        x = -1.0 + 2 * nn.sigmoid(x)

        return x


class Autoencoder(nn.Module):
    """A simple CNN autoencoder."""

    img_shape: Tuple[int, int, int]
    latent_dim: int
    strides: Tuple[int, int] = (1, 1)
    nonlinearity: Callable = nn.leaky_relu

    def setup(self):
        self.encoder = Encoder(
            latent_dim=self.latent_dim,
            strides=self.strides,
            nonlinearity=self.nonlinearity,
        )

        # the size of the image after the convolutional encoder, but before the dense layers
        # currently, we are using 2 convolutional layers
        downsampled_img_dim = (
            int(self.img_shape[0] / (self.strides[0] ** 2)),
            int(self.img_shape[1] / (self.strides[0] ** 2)),
            self.dims[-1],
        )
        print("Computed downsampled image dimension:", downsampled_img_dim)

        self.decoder = Decoder(
            downsampled_img_dim=downsampled_img_dim,
            strides=self.strides,
            nonlinearity=self.nonlinearity,
        )

    def __call__(self, x):
        z = self.encoder(x)
        x_rec = self.decoder(z)

        return x_rec

    def encode(self, x):
        return self.encoder(x)

    def decode(self, x):
        return self.decoder(x)
