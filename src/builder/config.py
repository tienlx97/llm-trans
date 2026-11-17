"""
Builder Agent Configuration - EPUB output settings.

Language-agnostic configuration for EPUB building.
Language-specific settings (titles, TOC labels) come from manifest.json.

The live stylesheet is DEFAULT_CSS in agent.py, selected per device profile
via _create_stylesheet(). Embedded fonts are per target language
(project.languages.<lang>.epub_fonts), not a builder-level setting.
"""

from typing import Any, Dict

from src.Deepseek.common.config import load_config


# ============================================================================
# EPUB FORMAT SETTINGS
# ============================================================================

def get_epub_version() -> str:
    """
    Get EPUB version from configuration.

    Returns:
        'EPUB2' or 'EPUB3'
    """
    config = load_config()
    version = config.get('builder', {}).get('epub_version', '3.0')
    # Normalize version string
    if version in ('3.0', '3', 'EPUB3'):
        return 'EPUB3'
    elif version in ('2.0', '2', 'EPUB2'):
        return 'EPUB2'
    return 'EPUB3'  # Default to EPUB3


# ============================================================================
# IMAGE PROCESSING
# ============================================================================

IMAGE_DEFAULTS: Dict[str, Any] = {
    'max_width_px': 1200,
    'max_height_px': 1800,
    'jpeg_quality': 85,
}


def get_image_config() -> Dict[str, Any]:
    """
    Get image processing configuration from config.yaml's builder.images.

    Consumed by device_profiles.resolve_profile() for the 'standard' profile.
    Partial config is merged over the defaults.

    Returns:
        Dict with max_width_px, max_height_px and jpeg_quality.
    """
    config = load_config()
    configured = config.get('builder', {}).get('images', {}) or {}

    merged = dict(IMAGE_DEFAULTS)
    merged.update({k: v for k, v in configured.items() if v is not None})
    return merged


def get_xtc_config() -> Dict[str, Any]:
    """
    Get the optional XTC export configuration from config.yaml's builder.xtc.

    Empty by default: XTC export is opt-in and delegates to an external Node
    renderer that is not vendored here. See src/builder/xtc_export.py for what
    the keys mean and why the trade is what it is.
    """
    config = load_config()
    return dict(config.get('builder', {}).get('xtc', {}) or {})


def get_device_profile_name() -> str:
    """
    Get the default device profile name from config.yaml's
    builder.device_profile.

    Returns:
        Profile name; 'standard' when unset. Validation happens in
        device_profiles.resolve_profile(), which owns the registry and can
        report the valid options.
    """
    config = load_config()
    name = config.get('builder', {}).get('device_profile', 'standard')
    return str(name).strip().lower() if name else 'standard'


# ============================================================================
# PARAGRAPH FORMATTING
# ============================================================================

# Collapse consecutive blank lines
COLLAPSE_BLANK_LINES = True

# Keep 1 in N consecutive blank lines (1=all, 2=every other)
BLANK_LINE_FREQUENCY = 2
