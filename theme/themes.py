from dataclasses import dataclass

@dataclass(frozen=True)
class Theme:
    # core tokens you already use
    BG: str
    TXT: str
    TXT_STRONG: str
    CARD_BG: str
    CARD_BORDER: str
    PLOT_FG: str
    BTN_BG: str
    BTN_BG_DOWN: str
    BTN_BORDER: str
    GOOD: str
    BAD: str
    GRAY: str
    PLOT_BG: str

DARK = Theme(
    BG="#0f1b22", TXT="#EAF2FF", TXT_STRONG="#000000",
    CARD_BG="#132530", CARD_BORDER="#1f3642", PLOT_FG="#7fdbff", 
    BTN_BG="#142a36", BTN_BG_DOWN="#0e2029", BTN_BORDER="#224050",
    GOOD="#2ecc71", BAD="#ff4136", GRAY="#7f8c8d", PLOT_BG="#0f1b22",
)

LIGHT = Theme(
    BG="#f6f8fb", TXT="#17212b", TXT_STRONG="#0b1117",
    CARD_BG="#ffffff", CARD_BORDER="#d5dde6", PLOT_FG="#8a99a6", 
    BTN_BG="#eef2f7", BTN_BG_DOWN="#e2e8f0", BTN_BORDER="#cbd5e1",
    GOOD="#1f9d55", BAD="#cc2936", GRAY="#8a99a6", PLOT_BG="#f6f8fb", 
)
SUB_DRK = Theme(
    BG="#0b2a38", TXT="white", TXT_STRONG="#000000", 
    CARD_BG="#0e3b4e", CARD_BORDER="#2d6f88", PLOT_FG="#7fdbff", 
    BTN_BG="#0e3b4e", BTN_BG_DOWN="#11475e",BTN_BORDER="#2d6f88",
    GOOD="#2ecc40", BAD="#b71c1c", GRAY="#7f8c8d", PLOT_BG="#0b2a38", 
)

THEMES = {"Dark": DARK, "Light": LIGHT, "Submarine Dark" : SUB_DRK}
DEFAULT_THEME = "Dark"
