import base64

import pytest

from portacode.connection.handlers.file_handlers import ImageReadHandler


def test_image_read_returns_validated_bounded_image(tmp_path):
    content = b"\x89PNG\r\n\x1a\n" + b"payload"
    image = tmp_path / "photo.png"
    image.write_bytes(content)

    result = ImageReadHandler(None, {}).execute({"path": str(image)})

    assert result["event"] == "image_read_response"
    assert result["mime_type"] == "image/png"
    assert base64.b64decode(result["content_base64"]) == content


def test_image_read_rejects_non_images(tmp_path):
    document = tmp_path / "not-an-image.png"
    document.write_text("hello")

    with pytest.raises(ValueError, match="not a supported image"):
        ImageReadHandler(None, {}).execute({"path": str(document)})
