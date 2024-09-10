import base64
import io
from typing import Union

import requests
from PIL import Image
from pydantic import BeforeValidator, PlainSerializer, SerializationInfo
from typing_extensions import Annotated


def download_image(url: str) -> Image.Image:
    try:
        # Make a request to download the image
        response = requests.get(url)
        response.raise_for_status()  # Raise an error for bad responses (4xx, 5xx)

        # Convert the image content to a PIL Image
        img = Image.open(io.BytesIO(response.content))
        return img

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Error downloading the image from {url}: {e}.")
    except Exception as e:
        raise RuntimeError(f"Error converting to PIL image: {e}")


def maybe_load_image_from_str_or_bytes(x: Union[Image.Image, str, bytes]) -> Image.Image:
    if isinstance(x, Image.Image):
        return x
    if isinstance(x, bytes):
        try:
            return Image.open(io.BytesIO(x))
        except Exception:
            raise RuntimeError("Encountered an error when loading image from bytes.")

    try:
        image = Image.open(io.BytesIO(base64.b64decode(x.encode("ascii"))))
        return image
    except Exception as e:
        raise RuntimeError(
            f"Encountered an error when loading image from bytes starting "
            f"with '{x[:20]}'. Expected either a PIL.Image.Image or a base64 "
            f"encoded string of bytes."
        ) from e


def serialize_image_to_byte_str(im: Image.Image, info: SerializationInfo) -> str:
    stream = io.BytesIO()
    im_format = im.format or "PNG"
    im.save(stream, format=im_format)
    im_b64 = base64.b64encode(stream.getvalue()).decode("ascii")
    if info.context and (max_image_b64_len := info.context.get("max_image_b64_len")):
        return im_b64[:max_image_b64_len] + "..."
    return im_b64


# A normal PIL image that supports serialization to b64 bytes string
SerializableImage = Annotated[
    Image.Image,
    BeforeValidator(maybe_load_image_from_str_or_bytes),
    PlainSerializer(serialize_image_to_byte_str),
]