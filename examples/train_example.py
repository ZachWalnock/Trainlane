from __future__ import annotations

import math
import random
import time

from expkit import trainer


def train() -> None:
    for step in range(1, 51):
        loss = math.exp(-step / 18.0) + random.uniform(-0.02, 0.02)
        acc = 1.0 - loss + random.uniform(-0.01, 0.01)

        trainer.log("loss", max(loss, 0.0001), step=step)
        trainer.log("accuracy", min(max(acc, 0.0), 1.0), step=step)

        print(f"step={step} loss={loss:.4f} acc={acc:.4f}")
        time.sleep(0.1)


if __name__ == "__main__":
    trainer.run(train)
