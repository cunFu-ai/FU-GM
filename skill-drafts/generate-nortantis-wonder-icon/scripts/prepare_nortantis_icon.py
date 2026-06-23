#!/usr/bin/env python3
"""Convert chroma-key art into a Nortantis black-ink alpha mask."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--key-color",
        default="#00ff00",
        help="Background color to remove, for example #00ff00 or #ff00ff.",
    )
    parser.add_argument("--alpha-max", type=int, default=205)
    parser.add_argument("--padding-ratio", type=float, default=0.04)
    parser.add_argument("--matte-floor", type=float, default=0.08)
    parser.add_argument("--transparent-threshold", type=float, default=20.0)
    parser.add_argument("--opaque-threshold", type=float, default=135.0)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def parse_hex_color(value: str) -> tuple[int, int, int]:
    text = value.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) != 6:
        raise ValueError(f"Invalid --key-color: {value!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def channel_to_ink(
    pixel: tuple[int, int, int, int],
    *,
    key_color: tuple[int, int, int],
    alpha_max: int,
    matte_floor: float,
    transparent_threshold: float,
    opaque_threshold: float,
) -> tuple[int, int, int, int]:
    red, green, blue, source_alpha = pixel

    key_red, key_green, key_blue = key_color
    distance = (
        (red - key_red) ** 2
        + (green - key_green) ** 2
        + (blue - key_blue) ** 2
    ) ** 0.5
    if distance <= transparent_threshold:
        matte = 0.0
    elif distance >= opaque_threshold:
        matte = 1.0
    else:
        matte = (distance - transparent_threshold) / (
            opaque_threshold - transparent_threshold
        )
    matte = max(0.0, min(1.0, (matte - matte_floor) / (1.0 - matte_floor)))
    matte *= source_alpha / 255.0

    luma = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    ink = max(0.0, min(1.0, (245.0 - luma) / 205.0))
    alpha = round(matte * ink * alpha_max)
    return 0, 0, 0, alpha


def prepare_icon(
    source: Path,
    output: Path,
    key_color: tuple[int, int, int],
    alpha_max: int,
    padding_ratio: float,
    matte_floor: float,
    transparent_threshold: float,
    opaque_threshold: float,
) -> dict[str, object]:
    image = Image.open(source).convert("RGBA")
    pixels = [
        channel_to_ink(
            pixel,
            key_color=key_color,
            alpha_max=alpha_max,
            matte_floor=matte_floor,
            transparent_threshold=transparent_threshold,
            opaque_threshold=opaque_threshold,
        )
        for pixel in image.getdata()
    ]
    result = Image.new("RGBA", image.size, (0, 0, 0, 0))
    result.putdata(pixels)

    bbox = result.getbbox()
    if not bbox:
        raise ValueError("No visible ink remained after chroma extraction")

    result = result.crop(bbox)
    padding = max(8, round(max(result.size) * padding_ratio))
    padded = Image.new(
        "RGBA",
        (result.width + padding * 2, result.height + padding * 2),
        (0, 0, 0, 0),
    )
    padded.alpha_composite(result, (padding, padding))

    output.parent.mkdir(parents=True, exist_ok=True)
    padded.save(output)

    alpha = padded.getchannel("A")
    minimum, maximum = alpha.getextrema()
    visible = sum(1 for value in alpha.getdata() if value)
    coverage = visible / (padded.width * padded.height)
    corners = [
        alpha.getpixel((0, 0)),
        alpha.getpixel((padded.width - 1, 0)),
        alpha.getpixel((0, padded.height - 1)),
        alpha.getpixel((padded.width - 1, padded.height - 1)),
    ]
    if maximum > alpha_max or any(corners):
        raise ValueError("Output failed alpha validation")

    return {
        "output": str(output),
        "mode": padded.mode,
        "width": padded.width,
        "height": padded.height,
        "key_color": f"#{key_color[0]:02x}{key_color[1]:02x}{key_color[2]:02x}",
        "alpha_min": minimum,
        "alpha_max": maximum,
        "visible_coverage": round(coverage, 4),
        "transparent_corners": True,
    }


def main() -> None:
    args = parse_args()
    if args.output.exists() and not args.force:
        raise FileExistsError(f"Output already exists: {args.output}")
    if not 1 <= args.alpha_max <= 255:
        raise ValueError("--alpha-max must be between 1 and 255")
    if not 0.0 <= args.matte_floor < 1.0:
        raise ValueError("--matte-floor must be between 0 and 1")
    if args.opaque_threshold <= args.transparent_threshold:
        raise ValueError("--opaque-threshold must be greater than --transparent-threshold")
    key_color = parse_hex_color(args.key_color)
    summary = prepare_icon(
        source=args.input,
        output=args.output,
        key_color=key_color,
        alpha_max=args.alpha_max,
        padding_ratio=args.padding_ratio,
        matte_floor=args.matte_floor,
        transparent_threshold=args.transparent_threshold,
        opaque_threshold=args.opaque_threshold,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
