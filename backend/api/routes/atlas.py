from __future__ import annotations

import functools
import json
import logging
import re
from urllib.parse import unquote

import nibabel
import numpy as np
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse
from matplotlib.colors import ListedColormap
from nilearn.datasets import fetch_atlas_harvard_oxford
from nilearn.image import load_img
from nilearn.plotting import view_img_on_surf

from backend.api.limiter import limiter
from backend.app.corpus.conditions import CONDITIONS

logger = logging.getLogger(__name__)
router = APIRouter()

# Mirrors ANCHOR_DEFS in frontend/src/lib/brain-anchors.ts. Kept in sync by
# hand since the two sides render with different libraries (Plotly mesh3d
# here vs. a hand-rolled canvas there); if you add/rename an anchor there,
# update this list too.
ANCHOR_DEFS: list[tuple[str, str, list[str]]] = [
    ("frontal", "Frontal lobe", ["frontal", "precentral"]),
    ("temporal", "Temporal lobe", ["temporal"]),
    ("parietal", "Parietal lobe", ["parietal", "postcentral", "angular"]),
    ("occipital", "Occipital lobe", ["occipital"]),
    ("insular", "Insular cortex", ["insular", "insula"]),
    ("cingulate", "Cingulate cortex", ["cingulate"]),
    ("subcortical", "Basal ganglia / subcortical nuclei", ["pallidum", "caudate", "putamen", "thalamus", "basal ganglia", "subcortical"]),
    ("midbrain", "Brainstem / midbrain", ["midbrain", "brainstem", "brain-stem", "pons", "medulla"]),
]

ANCHOR_COLORS: dict[str, str] = {
    "frontal": "#A78BFA",
    "temporal": "#F472B6",
    "parietal": "#818CF8",
    "occipital": "#C084FC",
    "insular": "#F0ABFC",
    "cingulate": "#FDA4AF",
    "subcortical": "#FB7185",
    "midbrain": "#FBBF24",
}

ATLAS_BACKGROUND_HEX = "#16131C"


def _match_anchor_id(label: str) -> str | None:
    lower = label.lower()
    for anchor_id, _, keywords in ANCHOR_DEFS:
        if any(kw in lower for kw in keywords):
            return anchor_id
    return None


@functools.lru_cache(maxsize=2)
def _load_atlas(atlas_name: str):
    return fetch_atlas_harvard_oxford(atlas_name)


def _find_region_index(atlas, region_name: str) -> int | None:
    for i, label in enumerate(atlas.labels):
        if label == region_name:
            return i
        if region_name.lower() in label.lower():
            return i
    return None


def _parse_atlas_label(atlas_label: str) -> str:
    tokens = re.split(r'[,;]', atlas_label)
    first_token = tokens[0].strip()
    clean_label = first_token.split("(")[0].strip()
    return clean_label


def _patch_hover_text_in_html(html: str, region_name: str, condition_name: str, highlight_hex: str) -> str:
    # view_img_on_surf's mesh3d trace has no per-vertex hover option in its
    # public API. Its JS template bakes each vertex's color into a plain hex
    # string array (info.vertexcolor) with no separate label field, so hover
    # text is derived here by matching each vertex's own baked-in color
    # against the highlight color we chose server-side, rather than patching
    # in a value nilearn never exposes.
    hover_text_json = json.dumps(f"{condition_name} ({region_name})")
    target_pattern = "info.vertexcolor = surfaceMapInfo['vertexcolor_' + hemisphere]"
    patch = (
        "info.vertexcolor = surfaceMapInfo['vertexcolor_' + hemisphere]\n"
        f"  var highlightHoverText = {hover_text_json}\n"
        f"  var highlightColor = {json.dumps(highlight_hex.lower())}\n"
        "  info.hovertext = info.vertexcolor.map(function (c) {\n"
        "    return c.toLowerCase() === highlightColor ? highlightHoverText : ''\n"
        "  })\n"
        "  info.hoverinfo = 'text'"
    )
    return html.replace(target_pattern, patch)

def _inject_atlas_bridge(html: str, color_map: dict[str, dict[str, str]]) -> str:
    # nilearn's mesh3d view has no "region clicked" event of its own - the
    # only per-vertex signal it exposes is info.vertexcolor (baked in at
    # render time from the cmap we colored this endpoint's mesh with). So
    # hover/click handlers here look up the picked vertex's color in a
    # color -> anchor table computed server-side, and postMessage the result
    # to the parent frame for BrainViewer to consume like a BrainCanvas hover.
    color_map_json = json.dumps(color_map)
    bridge_script = (
        "<script>"
        f"window.NLT_ANCHOR_MAP = {color_map_json};"
        "function nltPost(type, anchorId, label) {"
        "  try { window.parent.postMessage({source: 'neulittrace-atlas', type: type, anchorId: anchorId || null, label: label || null}, '*') } catch (e) {}"
        "}"
        "function nltPick(eventData, type) {"
        "  var pt = eventData && eventData.points && eventData.points[0];"
        "  if (!pt) { nltPost(type, null); return }"
        "  var color = ((pt.data && pt.data.vertexcolor && pt.data.vertexcolor[pt.pointNumber]) || '').toLowerCase();"
        "  var info = window.NLT_ANCHOR_MAP[color];"
        "  if (!info) { nltPost(type, null); return }"
        "  nltPost(type, info.id, info.label)"
        "}"
        "</script>"
    )
    html = html.replace("<head>", f"<head>{bridge_script}", 1)
    target_pattern = "Plotly.react(divId, data, layout, config)"
    patch = (
        "Plotly.react(divId, data, layout, config).then(function (gd) {" + "\n" +
        "    if (gd.__nltBound) return" + "\n" +
        "    gd.__nltBound = true" + "\n" +
        "    gd.on('plotly_hover', function (d) { nltPick(d, 'hover') })" + "\n" +
        "    gd.on('plotly_unhover', function () { nltPost('hover', null) })" + "\n" +
        "    gd.on('plotly_click', function (d) { nltPick(d, 'click') })" + "\n" +
        "  })"
    )
    return html.replace(target_pattern, patch)


def _inject_theme_style(html: str) -> str:
    # The plot renders at a fixed 3:2 box (nilearn's own getLayout()), so it
    # never exactly fills an arbitrary container, and its <select> controls
    # are separate flow siblings that would otherwise crowd it. Rather than
    # fighting Plotly's own sizing, give the whole frame a solid black
    # backdrop and center the fixed-size plot in the middle of it, so it
    # reads as roughly the same position/size as the 3D trace view.
    style = (
        "<style>"
        ".modebar { display: none !important; }"
        "html, body { margin: 0; padding: 0; height: 100%; background: #000; }"
        "body { font-family: 'Space Grotesk', sans-serif; display: flex; flex-direction: column; }"
        "body > main {"
        "flex: 1 1 auto; display: flex; align-items: center; justify-content: center;"
        "min-height: 0; background: #000; }"
        "#surface-plot { background: #000; }"
        "select#select-hemisphere, select#select-kind, select#select-view {"
        "position: absolute; top: 10px; z-index: 5; font-size: 10px; padding: 3px 6px;"
        "background: rgba(20,17,28,0.55); color: #E5E1EE; border: 1px solid rgba(255,255,255,0.15);"
        "border-radius: 6px; backdrop-filter: blur(6px); }"
        "select#select-hemisphere { left: 10px; }"
        "select#select-kind { left: 150px; }"
        "select#select-view { left: 260px; }"
        "</style>"
    )
    return html.replace("<head>", f"<head>{style}", 1)


_atlas_html_cache: dict[str | None, str] = {}
_query_atlas_html_cache: dict[tuple[str, ...], str] = {}


# Declared, not merely returned. Every branch of this route answers
# `text/html`, and without `response_class` FastAPI documents an
# `application/json` response: openapi-typescript then generates a client that
# parses HTML as JSON. Schemathesis reported it as an undocumented content type
# on all three atlas routes; backend/tests/test_api_contract.py had recorded the
# same gap in prose and left it open.
@router.get("/atlas", response_class=HTMLResponse)
@limiter.limit("20/minute")
def get_default_atlas(request: Request) -> Response:
    if None in _atlas_html_cache:
        return Response(content=_atlas_html_cache[None], media_type="text/html")

    try:
        atlas = _load_atlas("cort-maxprob-thr25-2mm")
        img = load_img(atlas.maps)
        data = img.get_fdata()

        # Cortical surface renders can't show subcortical/midbrain structures
        # (they don't reach the cortex mesh), so only cortical anchors ever
        # get a color/number here - that's expected, not a bug.
        anchor_order: list[str] = []
        region_to_anchor_num: dict[int, int] = {}
        for i, label in enumerate(atlas.labels):
            anchor_id = _match_anchor_id(label)
            if anchor_id is None:
                continue
            if anchor_id not in anchor_order:
                anchor_order.append(anchor_id)
            region_to_anchor_num[i] = anchor_order.index(anchor_id) + 1

        remapped_data = np.zeros_like(data)
        for region_index, anchor_num in region_to_anchor_num.items():
            remapped_data[data == region_index] = anchor_num
        remapped_img = nibabel.Nifti1Image(remapped_data.astype(np.float32), img.affine)

        anchor_labels = {anchor_id: label for anchor_id, label, _ in ANCHOR_DEFS}
        cmap = ListedColormap([ATLAS_BACKGROUND_HEX] + [ANCHOR_COLORS[a] for a in anchor_order])
        color_map = {
            ANCHOR_COLORS[a].lower(): {"id": a, "label": anchor_labels[a]} for a in anchor_order
        }

        view = view_img_on_surf(
            remapped_img,
            surf_mesh="fsaverage6",
            threshold=0.5,
            cmap=cmap,
            black_bg=True,
            symmetric_cmap=False,
            vmin=0,
            vmax=len(anchor_order),
            colorbar=False,
            vol_to_surf_kwargs={"interpolation": "nearest_most_frequent"},
        )

        html = _inject_atlas_bridge(view.html, color_map)
        html = _inject_theme_style(html)
        _atlas_html_cache[None] = html
        return Response(content=html, media_type="text/html")

    except Exception as e:
        logger.error(f"Error generating default atlas view: {e}", exc_info=True)
        return Response(
            content=(
                "<html><body style='font-family: sans-serif; padding: 20px;'>"
                "<p>Atlas view unavailable.</p>"
                "<p><small>Location reference from literature, not a diagnostic read.</small></p>"
                "</body></html>"
            ),
            status_code=200,
            media_type="text/html"
        )


@router.get("/atlas/query", response_class=HTMLResponse)
@limiter.limit("20/minute")
def get_query_atlas(request: Request, conditions: str = "") -> Response:
    try:
        condition_names = [unquote(n.strip()) for n in conditions.split(",") if n.strip()]

        cited_anchor_ids: set[str] = set()
        for name in condition_names:
            condition = next((c for c in CONDITIONS if c.name == name), None)
            if condition is None:
                continue
            clean_region = _parse_atlas_label(condition.atlas_label)
            anchor_id = _match_anchor_id(clean_region)
            if anchor_id is not None:
                cited_anchor_ids.add(anchor_id)

        if not cited_anchor_ids:
            return get_default_atlas(request)

        cache_key = tuple(sorted(cited_anchor_ids))
        if cache_key in _query_atlas_html_cache:
            return Response(content=_query_atlas_html_cache[cache_key], media_type="text/html")

        atlas = _load_atlas("cort-maxprob-thr25-2mm")
        img = load_img(atlas.maps)
        data = img.get_fdata()

        # Cortical surface renders can't show subcortical/midbrain structures
        # (they don't reach the cortex mesh), same limitation as the default
        # all-lobes view above - if every cited condition resolves to a
        # subcortical/midbrain lobe, anchor_order stays empty below and we
        # fall back to the default view rather than render an all-background
        # scene.
        anchor_order: list[str] = []
        region_to_anchor_num: dict[int, int] = {}
        for i, label in enumerate(atlas.labels):
            anchor_id = _match_anchor_id(label)
            if anchor_id is None or anchor_id not in cited_anchor_ids:
                continue
            if anchor_id not in anchor_order:
                anchor_order.append(anchor_id)
            region_to_anchor_num[i] = anchor_order.index(anchor_id) + 1

        if not anchor_order:
            return get_default_atlas(request)

        remapped_data = np.zeros_like(data)
        for region_index, anchor_num in region_to_anchor_num.items():
            remapped_data[data == region_index] = anchor_num
        remapped_img = nibabel.Nifti1Image(remapped_data.astype(np.float32), img.affine)

        anchor_labels = {anchor_id: label for anchor_id, label, _ in ANCHOR_DEFS}
        cmap = ListedColormap([ATLAS_BACKGROUND_HEX] + [ANCHOR_COLORS[a] for a in anchor_order])
        color_map = {
            ANCHOR_COLORS[a].lower(): {"id": a, "label": anchor_labels[a]} for a in anchor_order
        }

        view = view_img_on_surf(
            remapped_img,
            surf_mesh="fsaverage6",
            threshold=0.5,
            cmap=cmap,
            black_bg=True,
            symmetric_cmap=False,
            vmin=0,
            vmax=len(anchor_order),
            colorbar=False,
            vol_to_surf_kwargs={"interpolation": "nearest_most_frequent"},
        )

        html = _inject_atlas_bridge(view.html, color_map)
        html = _inject_theme_style(html)
        _query_atlas_html_cache[cache_key] = html
        return Response(content=html, media_type="text/html")

    except Exception as e:
        logger.error(f"Error generating query atlas view: {e}", exc_info=True)
        return Response(
            content=(
                "<html><body style='font-family: sans-serif; padding: 20px;'>"
                "<p>Atlas view unavailable.</p>"
                "<p><small>Location reference from literature, not a diagnostic read.</small></p>"
                "</body></html>"
            ),
            status_code=200,
            media_type="text/html"
        )


@router.get("/atlas/{condition_name}", response_class=HTMLResponse)
@limiter.limit("20/minute")
def get_atlas(request: Request, condition_name: str) -> Response:
    try:
        decoded_name = unquote(condition_name)
        if decoded_name in _atlas_html_cache:
            return Response(content=_atlas_html_cache[decoded_name], media_type="text/html")

        condition = next(
            (c for c in CONDITIONS if c.name == decoded_name),
            None
        )
        if not condition:
            return Response(
                content=(
                    "<html><body style='font-family: sans-serif; padding: 20px;'>"
                    "<p>Atlas view unavailable for this condition.</p>"
                    "<p><small>Location reference from literature, not a diagnostic read.</small></p>"
                    "</body></html>"
                ),
                status_code=200,
                media_type="text/html"
            )

        clean_region = _parse_atlas_label(condition.atlas_label)
        logger.info(f"Looking up region: {clean_region} for condition: {decoded_name}")

        atlas_cort = _load_atlas("cort-maxprob-thr25-2mm")
        region_index = _find_region_index(atlas_cort, clean_region)

        if region_index is None:
            atlas_sub = _load_atlas("sub-maxprob-thr25-2mm")
            region_index = _find_region_index(atlas_sub, clean_region)
            atlas = atlas_sub
        else:
            atlas = atlas_cort

        if region_index is None:
            logger.warning(f"Region not found in either atlas: {clean_region}")
            return Response(
                content=(
                    "<html><body style='font-family: sans-serif; padding: 20px;'>"
                    "<p>Atlas view unavailable for this condition.</p>"
                    "<p><small>Location reference from literature, not a diagnostic read.</small></p>"
                    "</body></html>"
                ),
                status_code=200,
                media_type="text/html"
            )

        img = load_img(atlas.maps)
        data = img.get_fdata()

        remapped_data = np.where(data == region_index, 2, np.where(data > 0, 1, 0))
        remapped_img = nibabel.Nifti1Image(remapped_data.astype(np.float32), img.affine)

        highlight_hex = "#FF5FA3"
        cmap = ListedColormap(["#16131C", "#8D8894", highlight_hex])

        view = view_img_on_surf(
            remapped_img,
            surf_mesh="fsaverage6",
            threshold=0.5,
            cmap=cmap,
            black_bg=True,
            symmetric_cmap=False,
            vmin=0,
            vmax=2,
            colorbar=False,
            vol_to_surf_kwargs={"interpolation": "nearest_most_frequent"},
        )

        html = _patch_hover_text_in_html(view.html, clean_region, condition.name, highlight_hex)
        matched_anchor_id = _match_anchor_id(clean_region)
        if matched_anchor_id is not None:
            html = _inject_atlas_bridge(html, {highlight_hex.lower(): {"id": matched_anchor_id, "label": clean_region}})
        html = _inject_theme_style(html)
        _atlas_html_cache[decoded_name] = html

        return Response(content=html, media_type="text/html")

    except Exception as e:
        logger.error(f"Error generating atlas view: {e}", exc_info=True)
        return Response(
            content=(
                "<html><body style='font-family: sans-serif; padding: 20px;'>"
                "<p>Atlas view unavailable for this condition.</p>"
                "<p><small>Location reference from literature, not a diagnostic read.</small></p>"
                "</body></html>"
            ),
            status_code=200,
            media_type="text/html"
        )
