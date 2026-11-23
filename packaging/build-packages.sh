#!/bin/bash
# Build dist/drive-loader_<version>_all.deb, .noarch.rpm and .pkg.tar.zst with nfpm.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
NFPM_VERSION=2.49.0
export VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' driveloader.py)
OUT=${OUT:-$ROOT/dist}
WORK=$(mktemp -d)
export STAGE=$WORK/stage
APP_ID=io.github.teiwazik.DriveLoader

mkdir -p "$STAGE/i18n" "$STAGE/icons" "$OUT"
cp driveloader.py LICENSE "$STAGE/"
cp i18n/*.json "$STAGE/i18n/"
cp data/icons/*.svg "$STAGE/icons/"
printf '#!/bin/sh\nexec python3 /usr/share/driveloader/driveloader.py "$@"\n' > "$STAGE/driveloader"
sed "s|@BINDIR@|/usr/bin|" data/$APP_ID.desktop.in > "$STAGE/$APP_ID.desktop"

NFPM=$(command -v nfpm || true)
if [ -z "$NFPM" ]; then
    base=https://github.com/goreleaser/nfpm/releases/download/v$NFPM_VERSION
    tarball=nfpm_${NFPM_VERSION}_Linux_x86_64.tar.gz
    curl -fsSL -o "$WORK/$tarball" "$base/$tarball"
    curl -fsSL -o "$WORK/checksums.txt" "$base/checksums.txt"
    ( cd "$WORK" && grep " $tarball\$" checksums.txt | sha256sum -c - )
    tar -xzf "$WORK/$tarball" -C "$WORK" nfpm
    NFPM=$WORK/nfpm
fi
# nfpm expands variables in some fields only, not in contents paths
sed -e "s|\${STAGE}|$STAGE|g" -e "s|\${VERSION}|$VERSION|g" packaging/nfpm.yaml > "$WORK/nfpm.yaml"
for p in deb rpm archlinux; do
    "$NFPM" package --config "$WORK/nfpm.yaml" --packager "$p" --target "$OUT/"
done
ls -l "$OUT"
