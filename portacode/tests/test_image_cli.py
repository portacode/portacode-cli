import base64
import json
from unittest.mock import Mock, patch

from portacode.image_cli import main


def _response():
    response = Mock(status_code=200)
    response.json.return_value = {
        "data": [{"b64_json": base64.b64encode(b"png-bytes").decode("ascii")}],
        "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        "size": "1024x1024",
        "quality": "low",
    }
    return response


@patch("portacode.image_cli.requests.post")
def test_generate_uses_metered_local_endpoint_and_saves_result(post, tmp_path, capsys):
    post.return_value = _response()
    output = tmp_path / "generated.png"

    assert main(["generate", "a lighthouse", "--out", str(output), "--quality", "low"]) == 0

    assert output.read_bytes() == b"png-bytes"
    assert post.call_args.args[0] == "http://127.0.0.1:61789/v1/images/generations"
    assert post.call_args.kwargs["json"]["model"] == "gpt-image-2"
    report = json.loads(capsys.readouterr().out)
    assert report["usage"]["total_tokens"] == 8


@patch("portacode.image_cli.requests.post")
def test_edit_uses_metered_multipart_endpoint(post, tmp_path):
    post.return_value = _response()
    source = tmp_path / "source.png"
    source.write_bytes(b"source")
    output = tmp_path / "edited.png"

    assert main([
        "edit", "make it blue", "--image", str(source), "--out", str(output)
    ]) == 0

    assert output.read_bytes() == b"png-bytes"
    assert post.call_args.args[0] == "http://127.0.0.1:61789/v1/images/edits"
    assert post.call_args.kwargs["data"]["model"] == "gpt-image-2"
    assert post.call_args.kwargs["files"][0][0] == "image[]"
