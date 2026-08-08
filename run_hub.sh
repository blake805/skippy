#!/bin/zsh
# Launches the Skippy hub with the production environment. Used by launchd
# (com.skippy.hub) when the hub needs to run outside the SkippyServer app.
#
# Secrets live outside the repo (this file is tracked). Currently that carries
# the reasoner API key and SKIPPY_ALLOW_CLOUD for the consult tool.
[ -f "$HOME/.skippy_secrets" ] && source "$HOME/.skippy_secrets"
export SKIPPY_WORKSPACE_ROOTS="/Users/blakeweinberg/skippy"
export SKIPPY_BIND_HOST="0.0.0.0"
export SKIPPY_VOICE_TOKEN="o7R1hWgJFNEGio2oz8nDyhKjlYVuhrQdsnmVhV5s"
export SKIPPY_VOICE_STT="mlx:mlx-community/parakeet-tdt-0.6b-v3"
export SKIPPY_VOICE_TTS="mlx:mlx-community/chatterbox-turbo-fp16"
export SKIPPY_VOICE_TTS_REF="/Users/blakeweinberg/skippy/voice_ref_studio.wav"
export SKIPPY_VOICE_OUT_RATE="24000"
# In-app code-edit approvals. The rebuilt SkippyMac understands the code_auth
# card, so the gate is live. (The iPhone app is still the old build until its
# keychain-signed rebuild lands, but chat/voice on the phone never triggers a
# coding run, so it is not affected.)
export SKIPPY_CODE_APPROVAL="app"
cd /Users/blakeweinberg/skippy
exec ./venv/bin/python skippy_factory.py
