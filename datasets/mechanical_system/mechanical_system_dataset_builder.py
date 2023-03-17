"""mechanical_system dataset."""
import dataclasses
import jax.numpy as jnp
from natsort import natsorted
from pathlib import Path
import tensorflow_datasets as tfds


@dataclasses.dataclass
class DatasetConfig(tfds.core.BuilderConfig):
    path: Path = None
    state_dim: int = None
    horizon_dim: int = 1
    img_size: tuple = (32, 32)


class Builder(tfds.core.GeneratorBasedBuilder):
    """DatasetBuilder for mechanical_system dataset."""

    VERSION = tfds.core.Version("1.0.0")
    RELEASE_NOTES = {
        "1.0.0": "Initial release.",
    }
    # pytype: disable=wrong-keyword-args
    BUILDER_CONFIGS = [
        # `name` (and optionally `description`) are required for each config
        DatasetConfig(
            name="single_pendulum_32x32px",
            description="Single pendulum dataset with images of size 32x32px.",
            path=Path("data") / "raw_datasets" / "single_pendulum_32x32px",
            state_dim=2,
            horizon_dim=10,
            img_size=(32, 32),
        ),
        DatasetConfig(
            name="single_pendulum_64x64px",
            description="Single pendulum dataset with images of size 64x64px.",
            path=Path("data") / "raw_datasets" / "single_pendulum_64x64px",
            state_dim=2,
            horizon_dim=10,
            img_size=(64, 64),
        ),
        DatasetConfig(
            name="double_pendulum_32x32px",
            description="Double pendulum dataset with images of size 32x32px.",
            path=Path("data") / "raw_datasets" / "double_pendulum_32x32px",
            state_dim=4,
            horizon_dim=10,
            img_size=(32, 32),
        ),
        DatasetConfig(
            name="double_pendulum_64x64px",
            description="Double pendulum dataset with images of size 64x64px.",
            path=Path("data") / "raw_datasets" / "double_pendulum_64x64px",
            state_dim=4,
            horizon_dim=10,
            img_size=(64, 64),
        ),
    ]
    # pytype: enable=wrong-keyword-args

    def _info(self) -> tfds.core.DatasetInfo:
        """Returns the dataset metadata."""
        return self.dataset_info_from_configs(
            features=tfds.features.FeaturesDict(
                {
                    # These are the features of your dataset like images, labels ...
                    "id": tfds.features.Scalar(dtype=jnp.int32),
                    "t_ts": tfds.features.Tensor(
                        shape=(self.builder_config.horizon_dim, ),
                        dtype=jnp.float64,
                    ),
                    "x_ts": tfds.features.Tensor(
                        shape=(
                            self.builder_config.horizon_dim,
                            self.builder_config.state_dim,
                        ),
                        dtype=jnp.float64,
                    ),
                    "rendering_ts": tfds.features.Sequence(
                        tfds.features.Image(
                            shape=(
                                self.builder_config.img_size[0],
                                self.builder_config.img_size[1],
                                3,
                            )
                        )
                    ),
                }
            ),
            # If there's a common (input, target) tuple from the
            # features, specify them here. They'll be used if
            # `as_supervised=True` in `builder.as_dataset`.
            supervised_keys=(
                "rendering_ts",
                "rendering_ts",
            ),  # Set to `None` to disable
            homepage="https://github.com/tud-cor-sr/learning-representations-from-first-principle-dynamics",
        )

    def _split_generators(self, dl_manager: tfds.download.DownloadManager):
        """Returns SplitGenerators."""
        return {
            "train": self._generate_examples(self.builder_config.path),
        }

    def _generate_examples(self, path: Path):
        """Yields examples."""
        # lazy imports
        cv2 = tfds.core.lazy_imports.cv2

        for sim_dir in sorted(path.iterdir()):
            sim_stem = sim_dir.stem
            sim_idx = int(sim_stem.lstrip("sim-"))

            labels_npz = jnp.load(sim_dir / "labels.npz")
            # convert to dict
            labels = {key:labels_npz[key] for key in labels_npz}

            rendering_ts = []
            for img_path in natsorted(sim_dir.glob("*.jpeg"), key=str):
                img = cv2.imread(str(img_path))
                rendering_ts.append(img)

            # merge labels with image and id
            sample = labels | {
                "id": sim_idx,
                "rendering_ts": rendering_ts,
            }

            yield sim_idx, sample
