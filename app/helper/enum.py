from enum import Enum


class ObjectNotFoundType(Enum):
    VBPL = 'vbpl'
    ANLE = 'anle'


class VbplTab(Enum):
    FULL_TEXT = 'toanvan'
    ATTRIBUTE = 'thuoctinh'
    RELATED_DOC = 'vanbanlienquan'