"""Machine-readable style presets for the PPTX generator.

Single source of truth for the *structured* half of the design system. The
human-facing prose lives in
``agent-image/builtin-skills/pptx-generator/references/design-system.md``
(baked into the agent image); this module is its faithful JSON mirror so the
frontend selector and the skill can consume the same values without parsing
Markdown tables.

These payloads are written into the shared library volume under ``styles/``
by :class:`~app.services.pptx_library.PptxLibrary`. They deliberately do NOT
live in the skill directory: the image entrypoint seeds skills per container,
which would copy them N times. The library volume is mounted read-only into
every container, so there is exactly one physical copy.
"""
from __future__ import annotations

STYLES_VERSION = 1

# --- Color palettes (design-system.md §Color Palette Reference) -------------
# `colors` keeps the document's original ordering; role assignment is left to
# the generator (the `tips` field carries the author's guidance).
_PALETTES: tuple[tuple[str, str, str, tuple[str, ...], str, str, str, bool], ...] = (
    # id, name, name_zh, colors, style, use_cases, tips, dark_mode_required
    ("modern-wellness", "Modern & Wellness", "现代疗愈",
     ("#006d77", "#83c5be", "#edf6f9", "#ffddd2", "#e29578"),
     "Fresh, soothing", "Healthcare, counseling, skincare, yoga/spa",
     "Deep teal for titles, light pink for background", False),
    ("business-authority", "Business & Authority", "商务权威",
     ("#2b2d42", "#8d99ae", "#edf2f4", "#ef233c", "#d90429"),
     "Formal, classic", "Annual reports, financial analysis, corporate intro, government",
     "Deep blue for professionalism, bright red to highlight data", False),
    ("nature-outdoors", "Nature & Outdoors", "自然户外",
     ("#606c38", "#283618", "#fefae0", "#dda15e", "#bc6c25"),
     "Grounded, earthy", "Outdoor gear, environmental, agriculture, historical culture",
     "Dark green base, cream text", False),
    ("vintage-academic", "Vintage & Academic", "复古学术",
     ("#780000", "#c1121f", "#fdf0d5", "#003049", "#669bbc"),
     "Classic, scholarly", "Academic lectures, history reviews, museums, heritage brands",
     "Strong contrast between deep red and deep blue", False),
    ("soft-creative", "Soft & Creative", "柔和创意",
     ("#cdb4db", "#ffc8dd", "#ffafcc", "#bde0fe", "#a2d2ff"),
     "Dreamy, candy-toned", "Mother & baby, desserts, women's fashion, kindergarten",
     "Use dark gray or black for text", False),
    ("bohemian", "Bohemian", "波西米亚",
     ("#ccd5ae", "#e9edc9", "#fefae0", "#faedcd", "#d4a373"),
     "Gentle, muted", "Wedding planning, home decor, organic food, slow living",
     "Cream background, green-brown accents", False),
    ("vibrant-tech", "Vibrant & Tech", "活力科技",
     ("#8ecae6", "#219ebc", "#023047", "#ffb703", "#fb8500"),
     "High energy, sporty", "Sports events, gyms, startup pitches, youth education",
     "Deep blue for stability, orange as focal accent", False),
    ("craft-artisan", "Craft & Artisan", "手作匠心",
     ("#7f5539", "#a68a64", "#ede0d4", "#656d4a", "#414833"),
     "Rustic, coffee-toned", "Coffee shops, handicrafts, traditional culture, bakery",
     "Suited for paper/leather textures", False),
    ("tech-night", "Tech & Night", "科技夜色",
     ("#000814", "#001d3d", "#003566", "#ffc300", "#ffd60a"),
     "Deep, luminous", "Tech launches, astronomy, night economy, luxury automobiles",
     "Must use dark mode", True),
    ("education-charts", "Education & Charts", "教育图表",
     ("#264653", "#2a9d8f", "#e9c46a", "#f4a261", "#e76f51"),
     "Clear, logical", "Statistical reports, education, market analysis, general business",
     "Perfect chart color scheme", False),
    ("forest-eco", "Forest & Eco", "森林生态",
     ("#dad7cd", "#a3b18a", "#588157", "#3a5a40", "#344e41"),
     "Monochrome gradient, forest", "Landscape design, ESG reports, environmental causes, botanical",
     "Monochrome palette is safe and cohesive", False),
    ("elegant-fashion", "Elegant & Fashion", "优雅时尚",
     ("#edafb8", "#f7e1d7", "#dedbd2", "#b0c4b1", "#4a5759"),
     "Muted, Morandi tones", "Haute couture, art galleries, beauty brands, magazine style",
     "Negative space is key", False),
    ("art-food", "Art & Food", "艺术美食",
     ("#335c67", "#fff3b0", "#e09f3e", "#9e2a2b", "#540b0e"),
     "Rich, vintage-poster", "Food documentaries, art exhibitions, ethnic themes, vintage restaurants",
     "Works well with large color blocks", False),
    ("luxury-mysterious", "Luxury & Mysterious", "奢华神秘",
     ("#22223b", "#4a4e69", "#9a8c98", "#c9ada7", "#f2e9e4"),
     "Cool, purple-toned", "Jewelry showcases, hotel management, high-end consulting, psychology",
     "Purple evokes premium atmosphere", False),
    ("pure-tech-blue", "Pure Tech Blue", "纯粹科技蓝",
     ("#03045e", "#0077b6", "#00b4d8", "#90e0ef", "#caf0f8"),
     "Futuristic, clean", "Cloud/AI, water/ocean, hospitals, clean energy",
     "Deep ocean to sky gradient", False),
    ("coastal-coral", "Coastal Coral", "海岸珊瑚",
     ("#0081a7", "#00afb9", "#fdfcdc", "#fed9b7", "#f07167"),
     "Refreshing, summery", "Travel, summer events, beverage brands, ocean themes",
     "Teal and coral as complementary focal colors", False),
    ("vibrant-orange-mint", "Vibrant Orange Mint", "活力橙薄荷",
     ("#ff9f1c", "#ffbf69", "#ffffff", "#cbf3f0", "#2ec4b6"),
     "Bright, cheerful", "Children's events, promotional posters, FMCG, social media",
     "Orange grabs attention, mint feels fresh", False),
    ("platinum-white-gold", "Platinum White Gold", "铂金白金",
     ("#0a0a0a", "#0070F3", "#D4AF37", "#f5f5f5", "#ffffff"),
     "Premium, professional", "Agent products, corporate websites, fintech, luxury brands",
     "White-gold base, blue for action, gold for emphasis", False),
)

PALETTES: list[dict] = [
    {
        "id": pid,
        "name": name,
        "name_zh": name_zh,
        "colors": list(colors),
        "style": style,
        "use_cases": [u.strip() for u in use_cases.split(",")],
        "tips": tips,
        "dark_mode_required": dark,
    }
    for pid, name, name_zh, colors, style, use_cases, tips, dark in _PALETTES
]

# --- Style recipes (design-system.md §Style Recipes) ------------------------
# Units are INCHES — PptxGenJS's native unit. Slide is 10" x 5.625" (16:9).
RECIPES: list[dict] = [
    {
        "id": "sharp",
        "name": "Sharp & Compact",
        "name_zh": "锐利紧凑",
        "character": "Geometric, high information density, formal and serious.",
        "best_for": "Data-dense, tables, professional reports",
        "corner_radius": {"small": 0.0, "medium": 0.03, "large": 0.05},
        "spacing": {
            "element_padding": [0.1, 0.15],
            "element_gap": [0.1, 0.2],
            "page_margin": 0.3,
            "block_gap": [0.25, 0.35],
        },
        # Component Style Mapping table — rectRadius per component.
        "components": {
            "button_tag": 0.0, "card_container": 0.03, "image_container": 0.0,
            "input_field": 0.0, "badge": 0.02, "avatar_frame": 0.0,
        },
        # Corner radius vs element height table.
        "radius_by_height": {"small": 0.0, "medium": 0.02, "large": 0.03, "xlarge": 0.05},
    },
    {
        "id": "soft",
        "name": "Soft & Balanced",
        "name_zh": "柔和均衡",
        "character": "Moderate rounding, comfortable whitespace, professional yet approachable.",
        "best_for": "Corporate, business presentations, general use",
        "corner_radius": {"small": 0.05, "medium": 0.08, "large": 0.12},
        "spacing": {
            "element_padding": [0.15, 0.2],
            "element_gap": [0.15, 0.25],
            "page_margin": 0.4,
            "block_gap": [0.35, 0.5],
        },
        "components": {
            "button_tag": 0.05, "card_container": 0.1, "image_container": 0.08,
            "input_field": 0.05, "badge": 0.05, "avatar_frame": 0.1,
        },
        "radius_by_height": {"small": 0.03, "medium": 0.05, "large": 0.08, "xlarge": 0.12},
    },
    {
        "id": "rounded",
        "name": "Rounded & Spacious",
        "name_zh": "圆润疏朗",
        "character": "Large corners, generous whitespace, friendly and modern.",
        "best_for": "Product intros, marketing, creative showcases",
        "corner_radius": {"small": 0.1, "medium": 0.15, "large": 0.25},
        "spacing": {
            "element_padding": [0.2, 0.3],
            "element_gap": [0.25, 0.4],
            "page_margin": 0.5,
            "block_gap": [0.5, 0.7],
        },
        "components": {
            "button_tag": 0.1, "card_container": 0.2, "image_container": 0.15,
            "input_field": 0.1, "badge": 0.08, "avatar_frame": 0.2,
        },
        "radius_by_height": {"small": 0.08, "medium": 0.12, "large": 0.2, "xlarge": 0.25},
    },
    {
        "id": "pill",
        "name": "Pill & Airy",
        "name_zh": "胶囊通透",
        "character": ("Full pill-shaped corners, abundant whitespace, light and open "
                      "feel, strong brand presence."),
        "best_for": "Brand showcases, launch events, premium presentations",
        "corner_radius": {"small": 0.2, "medium": 0.3, "large": 0.5},
        "spacing": {
            "element_padding": [0.25, 0.4],
            "element_gap": [0.3, 0.5],
            "page_margin": 0.6,
            "block_gap": [0.6, 0.9],
        },
        "components": {
            "button_tag": 0.2, "card_container": 0.3, "image_container": 0.25,
            "input_field": 0.2, "badge": 0.15, "avatar_frame": 0.5,
        },
        "radius_by_height": {"small": "height/2", "medium": "height/2",
                             "large": 0.3, "xlarge": 0.4},
        "pill_tip": "For a perfect pill shape set rectRadius = element height / 2.",
    },
]

# Quick Selection Guide — presentation type → recommended recipe ids.
RECIPE_SELECTION_GUIDE: list[dict] = [
    {"type": "Finance / Data reports", "recipes": ["sharp"],
     "reason": "High density, serious and precise"},
    {"type": "Corporate / Business", "recipes": ["soft"],
     "reason": "Balances professionalism and approachability"},
    {"type": "Product intro / Marketing", "recipes": ["rounded"],
     "reason": "Modern feel, friendly"},
    {"type": "Launch events / Brand", "recipes": ["pill"],
     "reason": "Premium feel, visual impact"},
    {"type": "Training / Education", "recipes": ["soft", "rounded"],
     "reason": "Clear, readable, friendly"},
    {"type": "Tech sharing", "recipes": ["sharp", "soft"],
     "reason": "Professional, information-dense"},
]

# --- Typography & spacing (design-system.md §Font/§Typography/§Spacing) -----
TYPOGRAPHY: dict = {
    "slide_size_inches": [10.0, 5.625],
    "fonts": {
        "chinese_default": "Microsoft YaHei",
        "english_default": "Arial",
        "english_alternatives": ["Georgia", "Calibri", "Cambria", "Trebuchet MS"],
        "mixed_rule": ("For mixed Chinese-English content use Microsoft YaHei for "
                       "Chinese and the chosen font for English."),
        "pairings": [
            {"header": "Georgia", "body": "Calibri"},
            {"header": "Arial Black", "body": "Arial"},
            {"header": "Calibri", "body": "Calibri Light"},
            {"header": "Cambria", "body": "Calibri"},
            {"header": "Trebuchet MS", "body": "Calibri"},
            {"header": "Impact", "body": "Arial"},
            {"header": "Palatino", "body": "Garamond"},
            {"header": "Consolas", "body": "Calibri"},
        ],
        "pairing_rule": ("Choose an interesting pairing — don't default to Arial for "
                         "everything. Header font gets personality, body font stays clean."),
    },
    "weight_rule": ("Plain body text and caption/legend text must NOT use bold. "
                    "Reserve bold for titles and headings only."),
    "type_scale_pt": {
        "annotation_source": [10, 12],
        "body_description": [14, 16],
        "subtitle": [18, 22],
        "title": [28, 36],
        "large_title": [44, 60],
        "data_callout": [60, 96],
    },
    "spacing_scale_inches": {
        "icon_to_text_gap": [0.08, 0.15],
        "list_item_spacing": [0.15, 0.25],
        "card_inner_padding": [0.2, 0.4],
        "element_group_gap": [0.3, 0.5],
        "page_safe_margin": [0.4, 0.6],
        "major_block_gap": [0.5, 0.8],
    },
    "density_zones": {
        "data_display": ["sharp", "soft"],
        "content_browsing": ["rounded", "pill"],
        "title_zone": ["soft", "rounded"],
    },
}

# --- Mandatory rules (design-system.md §Color Palette Rules) ----------------
RULES: dict = {
    "palette": [
        "Use ONLY the selected palette's colors. Do NOT create or modify colors.",
        "Do NOT adjust brightness, saturation or mix palette colors.",
        "Only exception: add transparency via the `transparency` property (0-100).",
    ],
    "forbidden": [
        "No gradients — solid colors only.",
        "No animations and no slide transitions — all slides are static.",
    ],
    "mixing": [
        "Outer container corner radius >= inner element corner radius.",
        "Information density drives spacing (see TYPOGRAPHY.density_zones).",
    ],
}


def styles_payload() -> dict:
    """Full style preset bundle served by ``GET /api/library/styles``."""
    return {
        "version": STYLES_VERSION,
        "source": "agent-image/builtin-skills/pptx-generator/references/design-system.md",
        "units": "inches (PptxGenJS native)",
        "palettes": PALETTES,
        "recipes": RECIPES,
        "recipe_selection_guide": RECIPE_SELECTION_GUIDE,
        "typography": TYPOGRAPHY,
        "rules": RULES,
    }


def palette_ids() -> list[str]:
    return [p["id"] for p in PALETTES]


def recipe_ids() -> list[str]:
    return [r["id"] for r in RECIPES]
