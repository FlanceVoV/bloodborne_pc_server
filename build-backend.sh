#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
command -v git >/dev/null
command -v cmake >/dev/null
if [ ! -d third_party/shadnet-p2p/.git ]; then
  git clone --recursive https://github.com/Wozzardman/shadnet-p2p.git third_party/shadnet-p2p
fi
git -C third_party/shadnet-p2p checkout 5a849a192fd4450593c57b10697316ac324c856e
git -C third_party/shadnet-p2p submodule update --init --recursive
cmake -S third_party/shadnet-p2p -B third_party/shadnet-p2p/build -DCMAKE_BUILD_TYPE=Release
cmake --build third_party/shadnet-p2p/build --parallel
printf '%s\n' 'Build complete. Set shadnet.executable and shadnet.enabled in config.json.'
