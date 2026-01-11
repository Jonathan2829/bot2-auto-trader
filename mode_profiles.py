# mode_profiles.py
# FUENTE DE VERDAD (BOT1 ALERTAS + BOT2 AUTO)
# - get_profile() SIEMPRE devuelve un Profile válido
# - apply_profile_to_globals() aplica el perfil al bot llamante

from dataclasses import dataclass

@dataclass
class Profile:
    # Riesgo/Reward mínimo
    RR_MIN: float

    # RSI para entrada
    RSI_ENTRADA_MIN: int
    RSI_ENTRADA_MAX: int

    # Volatilidad
    ATR_PCT_MIN: float          # ATR% mínimo
    STOP_ATR_MULT: float        # SL dinámico por ATR (si se usa)

    # Volumen / liquidez (proxy 5m)
    VOLUME_MULT: float          # vol_now / vol_avg
    MIN_QUOTE_VOL_5M: float     # volumen_quote (aprox) mínimo

    # Scalping
    SCALP_SL_PCT: float         # stop en %
    SCALP_LADDER: str           # "2:50,3.5:30,5:20" (tp%:share%)

    # Rango de precio permitido
    MIN_PRICE: float
    MAX_PRICE: float


PROFILES = {
    "CONSERVADOR": Profile(
        RR_MIN=1.40,
        RSI_ENTRADA_MIN=40,
        RSI_ENTRADA_MAX=65,
        ATR_PCT_MIN=0.40,
        STOP_ATR_MULT=1.4,
        VOLUME_MULT=1.15,
        MIN_QUOTE_VOL_5M=300_000,
        SCALP_SL_PCT=1.8,
        SCALP_LADDER="1.8:50,3:30,4.5:20",
        MIN_PRICE=0.05,
        MAX_PRICE=10_000,
    ),
    "NORMAL": Profile(
        RR_MIN=1.25,
        RSI_ENTRADA_MIN=38,
        RSI_ENTRADA_MAX=70,
        ATR_PCT_MIN=0.30,
        STOP_ATR_MULT=1.2,
        VOLUME_MULT=1.05,
        MIN_QUOTE_VOL_5M=150_000,
        SCALP_SL_PCT=2.0,
        SCALP_LADDER="2:50,3.5:30,5:20",
        MIN_PRICE=0.05,
        MAX_PRICE=10_000,
    ),
    "AGRESIVO": Profile(
        RR_MIN=1.10,
        RSI_ENTRADA_MIN=30,
        RSI_ENTRADA_MAX=75,
        ATR_PCT_MIN=0.20,
        STOP_ATR_MULT=1.0,
        VOLUME_MULT=1.00,
        MIN_QUOTE_VOL_5M=80_000,
        SCALP_SL_PCT=2.4,
        SCALP_LADDER="2.5:45,4:35,5.5:20",
        MIN_PRICE=0.05,
        MAX_PRICE=10_000,
    ),
}

def normalize_mode(mode_name: str) -> str:
    m = (mode_name or "NORMAL").upper().strip()
    if m in ("CONSERVATIVE", "SAFE"):
        return "CONSERVADOR"
    if m in ("AGGRESSIVE", "RISKY"):
        return "AGRESIVO"
    return m

def get_profile(mode_name: str) -> Profile:
    mode = normalize_mode(mode_name)
    prof = PROFILES.get(mode) or PROFILES.get("NORMAL")
    if prof is None:
        raise RuntimeError("ERROR CRÍTICO: PROFILES no contiene NORMAL")
    if not isinstance(prof, Profile):
        raise RuntimeError(f"Perfil inválido para modo {mode}: tipo={type(prof)} valor={prof}")
    return prof

def apply_profile_to_globals(mode_name: str, g: dict) -> Profile:
    p = get_profile(mode_name)
    for k, v in p.__dict__.items():
        g[k] = v
    return p

if __name__ == "__main__":
    for m in ("CONSERVADOR","NORMAL","AGRESIVO","unknown"):
        print(m, "=>", get_profile(m))
