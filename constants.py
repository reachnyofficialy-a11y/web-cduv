"""Shared constants for u3dconv.

Values come from the UnityFS format and Unity's TextureFormat enum. Where a
value is not verified against UnityPy's enums, the comment says so. The
UnityPy enums are the authority when they disagree.
"""

APP_NAME = "u3dconv"
APP_VERSION = "0.4.0"

# Default folder for extraction output. On iOS this should later be moved
# under the app's Documents/ directory.
DEFAULT_OUTPUT_DIR = "u3dconv_output"

# UnityFS bundles begin with this null-terminated ASCII signature.
UNITYFS_SIGNATURE = b"UnityFS\x00"

# The low 6 bits of the bundle flags field select the compression method.
COMPRESSION_MASK = 0x3F
COMPRESSION_NAMES = {
    0: "none",
    1: "LZMA",
    2: "LZ4",
    3: "LZ4HC",
    4: "LZHAM (unsupported)",
}

# Unity TextureFormat IDs. Only the common ones are named here.
TEXTURE_FORMAT_NAMES = {
    3: "RGB24",
    4: "RGBA32",
    5: "ARGB32",
    10: "ETC_RGB4",
    30: "PVRTC_RGB2",
    31: "PVRTC_RGBA2",
    32: "PVRTC_RGB4",
    33: "PVRTC_RGBA4",
    45: "ETC2_RGB",
    46: "ETC2_RGBA1",
    47: "ETC2_RGBA8",
}

# ASTC formats occupy a contiguous range in Unity's enum: RGB block sizes
# 4x4..12x12 first, then RGBA block sizes. Verify exact bounds against UnityPy
# before relying on them in a later stage.
ASTC_FORMAT_IDS = range(48, 66)

# Unity object types that are extracted as decoded files rather than raw bytes.
TEXTURE_TYPES = ("Texture2D",)
AUDIO_TYPES = ("AudioClip",)

# --- Stage 2: repacking -----------------------------------------------------

# Compression choices accepted by UnityPy's BundleFile.save(packer=...).
#   original - keep whatever compression the source bundle used
#   lz4      - fast to load, moderate size (default)
#   lzma     - smallest size, slowest to load
#   none     - uncompressed
PACKER_CHOICES = ("original", "lz4", "lzma", "none")
DEFAULT_PACKER = "lz4"

# Uncompressed format used when re-encoding to the original format fails.
# RGBA32 is supported by every Unity version and every encoder path.
FALLBACK_TEXTURE_FORMAT = "RGBA32"

# Default folder name for PNGs written by the `convert` command.
DEFAULT_TEXTURE_DIR = "u3dconv_textures"

# --- Stage 3: local Web UI ----------------------------------------------------

DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_MB = 512
