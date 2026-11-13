#!/bin/bash
# Gemeinsamer Teil der curl- und wget-Shims: Dateien aus diesem Fork (Zweig mhb)
# kommen aus dem lokalen Checkout statt aus dem Netz, damit der Build genau den
# lokalen Stand nimmt. Alles andere geht an das echte Programm.
FORK_RAW="https://raw.githubusercontent.com/MHB-Markt-und-Motor-GmbH/rdgen/refs/heads/mhb/"

# local_path URL -> Pfad im Checkout oder leer
local_path() {
  case "$1" in
    "$FORK_RAW"*) printf '%s/%s' "$RDGEN" "${1#"$FORK_RAW"}" ;;
  esac
}
