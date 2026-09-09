#!/bin/sh
# Install a verified release in the current user's account. Run again to update.
set -eu

die() { printf 'g64conv: %s\n' "$*" >&2; exit 1; }

case "${1:-}" in
  --help|-h)
    printf '%s\n' 'Usage: sh install.sh' \
      'Installs/updates ~/.local/bin/g64conv. Requires ffmpeg and ffprobe.' \
      'Optional: G64CONV_VERSION=v0.2.0, G64CONV_PREFIX=/absolute/path'
    exit 0 ;;
  '') ;;
  *) die "Unknown argument: $1 (use --help)" ;;
esac

case "$(uname -s)" in
  Darwin) platform=macos ;;
  Linux) platform=linux ;;
  *) die 'Supported systems: macOS and Linux. See README for Python installation.' ;;
esac
case "$(uname -m)" in
  arm64|aarch64) arch=arm64 ;;
  x86_64|amd64) arch=x86_64 ;;
  *) die 'Supported processors: x86_64 and ARM64. See README for Python installation.' ;;
esac

for tool in curl tar mktemp; do
  command -v "$tool" >/dev/null 2>&1 || die "Required command missing: $tool"
done
if command -v sha256sum >/dev/null 2>&1; then
  hasher=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  hasher=shasum
else
  die 'Install sha256sum (coreutils) or shasum before continuing.'
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  case "$platform" in
    macos) hint='brew install ffmpeg' ;;
    linux) hint='sudo apt install ffmpeg (Debian/Ubuntu), or your distribution package manager' ;;
  esac
  die "Install FFmpeg first: $hint. Then run this installer again."
fi

prefix=${G64CONV_PREFIX:-"$HOME/.local"}
case "$prefix" in /*) ;; *) die 'G64CONV_PREFIX must be an absolute path.' ;; esac
base=https://github.com/barkleesanders/g64conv/releases
version=${G64CONV_VERSION:-latest}
case "$version" in
  latest) base=$base/latest/download ;;
  v[0-9]*)
    case "$version" in *[!a-zA-Z0-9._-]*) die 'Invalid G64CONV_VERSION.' ;; esac
    base=$base/download/$version ;;
  *) die 'G64CONV_VERSION must be latest or a v-prefixed release tag.' ;;
esac

binary=$prefix/bin/g64conv
store=$prefix/lib/g64conv
if [ -e "$binary" ] || [ -L "$binary" ]; then
  [ -L "$binary" ] || die "$binary already exists and is not managed by this installer."
  case "$(readlink "$binary")" in
    "$store"/release.*/g64conv/g64conv) ;;
    *) die "$binary belongs to another installation. Remove or relocate it first." ;;
  esac
fi

work=$(mktemp -d)
stage=
cleanup() {
  rm -rf "$work"
  if [ -n "$stage" ]; then rm -rf "$stage"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
asset=g64conv-$platform-$arch.tar.gz
printf 'Downloading %s (%s)…\n' "$asset" "$version"
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$base/SHA256SUMS" -o "$work/SHA256SUMS"
curl --proto '=https' --tlsv1.2 -fsSL --retry 3 "$base/$asset" -o "$work/$asset"
expected=$(awk -v name="$asset" '$2 == name {print $1}' "$work/SHA256SUMS")
[ "${#expected}" -eq 64 ] || die "Missing or ambiguous checksum for $asset."
case "$expected" in *[!0-9a-fA-F]*) die 'Invalid SHA-256 checksum.' ;; esac
if [ "$hasher" = sha256sum ]; then
  actual=$(sha256sum "$work/$asset" | awk '{print $1}')
else
  actual=$(shasum -a 256 "$work/$asset" | awk '{print $1}')
fi
[ "$actual" = "$expected" ] || die 'Checksum mismatch; nothing installed. Retry in case a release changed during download.'

mkdir -p "$store" "$prefix/bin"
stage=$(mktemp -d "$store/release.XXXXXXXX")
tar -xzf "$work/$asset" -C "$stage"
[ -x "$stage/g64conv/g64conv" ] || die 'Release archive is missing its executable.'
"$stage/g64conv/g64conv" --version || die 'Executable cannot run on this system; previous installation preserved.'
# Keep previous versions so a running conversion survives an update.
ln -s "$stage/g64conv/g64conv" "$stage/new-link"
if [ -L "$binary" ]; then
  # The destination points to a file, never to a directory.
  mv -f "$stage/new-link" "$binary"
else
  mv -n "$stage/new-link" "$binary"
  [ ! -L "$stage/new-link" ] || die "Another installation appeared at $binary; left it intact."
fi
stage=
printf 'Installed: %s\nRun: "%s" gui\n' "$binary" "$binary"
case ":$PATH:" in
  *":$prefix/bin:"*) ;;
  *) printf 'Add this directory to your shell PATH: %s\n' "$prefix/bin"
     printf "For this terminal: export PATH=\"%s/bin:\$PATH\"\n" "$prefix" ;;
esac
printf '%s\n' 'To update, run this installer again. Older versions remain in the installation directory.'
