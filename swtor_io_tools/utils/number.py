# <pep8 compliant>

from struct import pack, unpack


def decodeHalfFloat(arg):
    # type: (int) -> float
    return unpack('<e', pack('<H', arg))[0]


def encodeHalfFloat(arg):
    # type: (float) -> int
    return unpack('<H', pack('<e', arg))[0]


def align(pos, alignment, prefix_size=0):
    # type: (int, int, int) -> int
    """
    Rounds pos up to the next multiple of alignment (a power of two), counted
    from prefix_size: file formats with a prefix (such as an 8-byte magic +
    version) often align their data relative to the end of it.
    """
    return ((pos - prefix_size + alignment - 1) & -alignment) + prefix_size
