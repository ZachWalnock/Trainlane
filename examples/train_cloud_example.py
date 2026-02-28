from __future__ import annotations

import math
import time

from expkit import trainer


def train() -> None:
    for step in range(1, 6):
        loss = math.exp(-step / 5.0)
        trainer.log("loss", loss, step=step)
        print(f"cloud-step={step} loss={loss:.4f}")
        time.sleep(1)


if __name__ == "__main__":
    trainer.run(
        train,
        cloud={
            "compute": "cpu",
            "model_size_b": 7,
            "dataset_size_gb": 1,
            "speed": "balanced",
            "use_spot": False,
        },
    )
