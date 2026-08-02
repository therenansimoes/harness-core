#!/usr/bin/env python3
"""Portão de aceite de qualquer unidade do website-faz-rogers.

Delega para o verify.sh do próprio repo (lint -> build -> 8 checks do HTML
gerado): a regra de qualidade mora no projeto, não em cópias divergentes aqui.
Roda com o workspace efêmero como cwd.
"""
import subprocess
import sys

r = subprocess.run(["bash", "verify.sh"], capture_output=True, text=True, timeout=110)
sys.stdout.write(r.stdout[-4000:])
sys.stderr.write(r.stderr[-4000:])
sys.exit(r.returncode)
