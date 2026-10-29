#!/bin/bash
# Builds the torrest daemon (and libtorrest) for LG webOS: armv7 with the soft-float ABI
# (gnueabi), using webosbrew's NDK (https://github.com/openlgtv/buildroot-nc4). Upstream's
# linux_armv7 builds are hard-float (armhf) and can't run there: they need
# /lib/ld-linux-armhf.so.3, and webOS only has /lib/ld-linux.so.3.
#
# Runs on x86_64 Linux (e.g. a GitHub Actions ubuntu runner). Needs cmake, make, git, wget, curl,
# zip, perl and binutils. Writes build/webos/torrest.<version>.linux_armv7_softfp.zip and
# libtorrest.<version>.linux_armv7_softfp.zip, named like torrest-cpp's release assets so
# build.sh can use them.
set -euo pipefail

TORREST_VERSION="${TORREST_VERSION:-v0.0.8}"
NDK_RELEASE="${NDK_RELEASE:-2026.08-webos}"
NDK_NAME="arm-webos-linux-gnueabi_sdk-buildroot"
TRIPLE="arm-webos-linux-gnueabi"
PLATFORM="linux_armv7_softfp"

# CMake 4 rejects projects declaring a minimum below 3.5 (oatpp 1.3.0 does)
export CMAKE_POLICY_VERSION_MINIMUM=3.5

repo_path="$(dirname "$(dirname "$(realpath "${0}")")")"
out_path="${repo_path}/build/webos"
work="${WORK_DIR:-${HOME}/webos-build}"
jobs="$(nproc)"
mkdir -p "${work}" "${out_path}"

info() { echo "$(date '+%H:%M:%S') - ${1}"; }

# Toolchain
sdk="${work}/${NDK_NAME}"
if [ ! -x "${sdk}/bin/${TRIPLE}-gcc" ]; then
  info "Downloading the webOS NDK (${NDK_RELEASE})"
  curl -fsSL "https://github.com/openlgtv/buildroot-nc4/releases/download/${NDK_RELEASE}/${NDK_NAME}-x86_64.tar.gz" |
    tar -C "${work}" -xz
  "${sdk}/relocate-sdk.sh"
fi
export PATH="${sdk}/bin:${PATH}"
sysroot="$("${TRIPLE}-gcc" -print-sysroot)"
info "Compiler: $("${TRIPLE}-gcc" --version | head -1), sysroot ${sysroot}"

# Our dependencies go in their own prefix, searched before the NDK's sysroot (which has its
# own, shared, openssl and boost)
prefix="${work}/deps"
toolchain="${work}/toolchain.cmake"
cat >"${toolchain}" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_SYSROOT "${sysroot}")
set(CMAKE_C_COMPILER "${sdk}/bin/${TRIPLE}-gcc")
set(CMAKE_CXX_COMPILER "${sdk}/bin/${TRIPLE}-g++")
set(CMAKE_FIND_ROOT_PATH "${prefix}" "${sysroot}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
EOF

src="${work}/torrest-cpp"
if [ ! -d "${src}" ]; then
  info "Cloning torrest-cpp ${TORREST_VERSION}"
  git clone -q --depth 1 --branch "${TORREST_VERSION}" https://github.com/i96751414/torrest-cpp.git "${src}"
fi

if [ ! -f "${prefix}/.done" ]; then
  info "Building dependencies"
  # The same options as torrest-cpp's docker/linux-armv7.Dockerfile
  cmake_opts="-DCMAKE_POSITION_INDEPENDENT_CODE=ON"
  PREFIX="${prefix}" CMD="env" CMAKE_TOOLCHAIN_FILE="${toolchain}" \
    RANGE_PARSER_OPTS="${cmake_opts}" NLOHMANN_JSON_OPTS="${cmake_opts}" SPDLOG_OPTS="${cmake_opts}" \
    OATPP_OPTS="-DOATPP_LINK_ATOMIC=OFF ${cmake_opts}" OATPP_SWAGGER_OPTS="${cmake_opts}" \
    BOOST_CONFIG="using gcc : : ${TRIPLE}-g++ ;" BOOST_OPTS="target-os=linux cxxflags=-fPIC cflags=-fPIC" \
    OPENSSL_OPTS="linux-armv4" OPENSSL_CROSS_COMPILE="${TRIPLE}-" LIBTORRENT_OPTS="${cmake_opts}" \
    "${src}/scripts/install_dependencies.sh" --static -j "${jobs}"
  touch "${prefix}/.done"
fi

# The C++ runtime is linked in, so only libc/libm/libpthread/libdl come from the TV
build() {
  local name="${1}" type="${2}"
  shift 2
  info "Building ${name}"
  mkdir -p "${work}/out-${name}"
  cmake -S "${src}" -B "${work}/build-${name}" -DCMAKE_BUILD_TYPE=Release -DCMAKE_TOOLCHAIN_FILE="${toolchain}" \
    -DBoost_USE_STATIC_LIBS=ON -DOPENSSL_USE_STATIC_LIBS=ON -Dtarget_path="${work}/out-${name}/" \
    -DCMAKE_"${type}"_LINKER_FLAGS="-static-libstdc++ -static-libgcc" "$@"
  cmake --build "${work}/build-${name}" --target torrest -- -j "${jobs}"
}
build torrest EXE
build libtorrest SHARED -Dbuild_library=ON

# Checks: soft-float, the soft-float loader, and the newest glibc symbol version needed
check() {
  local file="${1}"
  info "Checking $(basename "${file}")"
  "${TRIPLE}-readelf" -h "${file}" | grep -E "Machine|Flags"
  "${TRIPLE}-readelf" -l "${file}" | grep -i "interpreter" || true
  if "${TRIPLE}-readelf" -A "${file}" | grep -q "Tag_ABI_VFP_args: VFP registers"; then
    echo "ERROR: ${file} uses the hard-float ABI" >&2
    exit 1
  fi
  "${TRIPLE}-readelf" -d "${file}" | grep NEEDED
  echo "Newest glibc version needed: $("${TRIPLE}-objdump" -T "${file}" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -1)"
}
check "${work}/out-torrest/torrest"
check "${work}/out-libtorrest/libtorrest.so"

version="${TORREST_VERSION#v}"
(cd "${work}/out-torrest" && zip -9 -q "${out_path}/torrest.${version}.${PLATFORM}.zip" torrest)
(cd "${work}/out-libtorrest" && zip -9 -q "${out_path}/libtorrest.${version}.${PLATFORM}.zip" libtorrest.so)
info "Done: $(ls "${out_path}")"
