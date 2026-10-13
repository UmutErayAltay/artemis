"""Obsidian vault hafızası: sözcük araması, isteğe bağlı embedding ve güvenli yol çözümü.

Bu paket yalnız okuma ve yol mantığını taşır; araçlar `jarvis/tools/plugins/memory.py`
içindedir ve her çağrı `SafetyGate` üzerinden geçer (ADR 0015). Hafıza kapalı gelir.
"""
