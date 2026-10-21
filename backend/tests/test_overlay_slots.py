"""Overlay images one by one: each badge uploaded into its slot, named as the renderer expects."""

import io

from PIL import Image

from coverarr.services.overlay_slots import catalogue, guess
from coverarr.services.overlays import build_plan, pack_dir
from tests.conftest import run_job
from tests.test_flow import add_servers


def _png(color=(255, 0, 0, 255), fmt="PNG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA" if fmt == "PNG" else "RGB", (80, 40), color[: 4 if fmt == "PNG" else 3]).save(
        buf, fmt
    )
    return buf.getvalue()


def test_guess_slot_from_file_name():
    slots = catalogue({"libraries": {}}, ["Channel 4"])
    cases = {
        "4K Dolby Vision.png": "resolution/4kdv.png",
        "UHD DV HDR10+.png": "resolution/4kdvhdrplus.png",
        "2160p-dv-hdr.png": "resolution/4kdvhdr.png",
        "1080p HDR10.png": "resolution/1080phdr.png",
        "720P.png": "resolution/720p.png",
        "SD.png": "resolution/sd.png",
        "480p HDR.webp": "resolution/sdhdr.png",
        "4k.png": "resolution/4k.png",
        "gradient_bottom.png": "gradient_bottom.png",
        "Gradient Episode Top.png": "gradient_episode_top.png",
        "Directors Cut.png": "edition/directors.png",
        "channel 4.png": "network-images/images/network/color/Channel 4.png",
        "holiday photo.png": None,
    }
    for name, expected in cases.items():
        assert guess(name, slots) == expected, name


async def test_upload_into_slots(app, authed, fake_servers):
    client = authed
    await add_servers(client)
    await run_job(app, client, "import")

    listing = (await client.get("/api/overlays/slots")).json()
    assert listing["pack"] is None
    slots = {s["path"]: s for s in listing["slots"]}
    assert {"resolution/4kdvhdr.png", "resolution/sd.png", "edition/directors.png"} <= set(slots)
    assert "network-images/images/network/color/Channel 4.png" in slots  # from the shows' networks
    assert all(s["current"] is None for s in slots.values())

    # The first image starts a pack of its own; a JPEG is stored as the slot's PNG.
    resp = await client.post(
        "/api/overlays/slots",
        files={"file": ("my 4k dv.jpg", _png(fmt="JPEG"), "image/jpeg")},
        data={"path": "resolution/4kdv.png"},
    )
    assert resp.status_code == 200, resp.text
    root = pack_dir(app.state.config.config_dir, "custom")
    assert (root / "resolution/4kdv.png").is_file()
    with Image.open(root / "resolution/4kdv.png") as img:
        assert img.format == "PNG"
    slots = {s["path"]: s for s in (await client.get("/api/overlays/slots")).json()["slots"]}
    assert slots["resolution/4kdv.png"]["exact"] is True
    # 4K DV + HDR has no image of its own yet: it falls back to the 4K DV badge.
    assert slots["resolution/4kdvhdr.png"]["current"] == "resolution/4kdv.png"
    assert slots["resolution/4kdvhdr.png"]["exact"] is False
    thumb = await client.get("/api/overlays/pack/file", params={"path": "resolution/4kdv.png"})
    assert thumb.status_code == 200
    assert (
        await client.get("/api/overlays/pack/file", params={"path": "../coverarr.db"})
    ).status_code == 422

    # An SD badge replaces the 480p/576p ones the renderer would otherwise pick first.
    (root / "resolution/480p.png").write_bytes(_png())
    (root / "resolution/SD.JPG").write_bytes(_png(fmt="JPEG"))
    resp = await client.post(
        "/api/overlays/slots",
        files={"file": ("sd.png", _png((0, 0, 255, 255)), "image/png")},
        data={"path": "resolution/sd.png"},
    )
    assert resp.status_code == 200, resp.text
    names = sorted(p.name for p in (root / "resolution").iterdir())
    assert names == ["4kdv.png", "sd.png"]
    plan = build_plan(
        root,
        "movie",
        [
            {
                "type": "resolution",
                "folder": "resolution",
                "enabled": True,
                "h_align": "left",
                "v_align": "top",
                "h_offset": 0,
                "v_offset": 0,
                "scale": 1,
            }
        ],
        {"resolution": "576p"},
    )
    assert plan.layers[0].path.endswith("resolution/sd.png")

    # Unknown slots and non-images are refused; a slot can be emptied.
    bad = await client.post(
        "/api/overlays/slots",
        files={"file": ("x.png", _png(), "image/png")},
        data={"path": "../../escape.png"},
    )
    assert bad.status_code == 422
    bad = await client.post(
        "/api/overlays/slots",
        files={"file": ("x.png", b"not an image", "image/png")},
        data={"path": "resolution/720p.png"},
    )
    assert bad.status_code == 422
    assert (
        await client.delete("/api/overlays/slots", params={"path": "resolution/sd.png"})
    ).status_code == 200
    assert not (root / "resolution/sd.png").exists()

    guesses = (
        await client.post("/api/overlays/slots/guess", json={"names": ["4K HDR.png", "cat.png"]})
    ).json()
    assert guesses == [
        {"name": "4K HDR.png", "path": "resolution/4khdr.png"},
        {"name": "cat.png", "path": None},
    ]
