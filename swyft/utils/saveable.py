from abc import ABC, abstractmethod
from os import name
from typing import TypeVar

import torch

from swyft.types import PathType

StateDictSaveableType = TypeVar("StateDictSaveableType", "StateDictSaveable")


class StateDictSaveable(ABC):
    @abstractmethod
    def state_dict(self) -> dict:
        return NotImplementedError()

    @abstractmethod
    def from_state_dict(self) -> StateDictSaveableType:
        return NotImplementedError()

    @classmethod
    def load(cls, filename: PathType) -> StateDictSaveableType:
        sd = torch.load(filename)
        return cls.from_state_dict(sd)

    def save(self, filename: PathType) -> None:
        sd = self.state_dict()
        torch.save(sd, filename)


if __name__ == "__main__":
    pass
