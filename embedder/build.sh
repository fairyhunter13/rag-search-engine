#!/bin/bash
# Build script for opencode-embedder Python binary
#
# Usage:
#   ./build.sh           # Build for current platform
#   ./build.sh --clean   # Clean build artifacts first
#
# Prerequisites:
#   pip install -e ".[build]"

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Detect platform
PLATFORM=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$PLATFORM" in
linux*) PLATFORM="linux" ;;
darwin*) PLATFORM="darwin" ;;
*) PLATFORM="unknown" ;;
esac

# Detect architecture
ARCH=$(uname -m)
case "$ARCH" in
x86_64) ARCH="x64" ;;
aarch64) ARCH="arm64" ;;
arm64) ARCH="arm64" ;;
*) ARCH="unknown" ;;
esac

# Get version from git
VERSION_DATE=$(date +%Y-%m-%d)
VERSION_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
VERSION="${VERSION_DATE}-${VERSION_COMMIT}"

echo -e "${BLUE}Building opencode-embedder ${VERSION} for ${PLATFORM}/${ARCH}${NC}"

# Clean if requested
if [[ "$1" == "--clean" ]]; then
  echo -e "${YELLOW}Cleaning build artifacts...${NC}"
  rm -rf build dist __pycache__ *.egg-info .venv
fi

# Create/activate virtual environment
VENV_DIR=".venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo -e "${YELLOW}Creating virtual environment...${NC}"
  python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q -e ".[build]"

# Install GPU support if available
# ROCm (AMD GPU) - Linux only
# IMPORTANT: We only install ONE onnxruntime package to avoid conflicts and massive build sizes
if [[ "$PLATFORM" == "linux" ]] && command -v rocm-smi &>/dev/null; then
  if rocm-smi --showid &>/dev/null 2>&1; then
    echo -e "${BLUE}ROCm GPU detected, configuring onnxruntime...${NC}"

    # Detect system ROCm version (follow full symlink chain)
    ROCM_VER=""
    if [[ -e /opt/rocm ]]; then
      # Follow all symlinks to get actual version directory
      rocm_target=$(readlink -f /opt/rocm 2>/dev/null)
      ROCM_VER=$(echo "$rocm_target" | grep -oE '[0-9]+\.[0-9]+' | head -1)
    fi
    if [[ -z "$ROCM_VER" ]]; then
      # Fallback: check for installed versioned directories
      ROCM_VER=$(ls -d /opt/rocm-* 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | sort -V | tail -1)
    fi
    echo -e "${BLUE}System ROCm version: ${ROCM_VER:-unknown}${NC}"

    # CRITICAL: Clean ALL existing onnxruntime packages first
    # Multiple onnxruntime packages cause PyInstaller to bundle 1.9GB+ of ROCm libraries
    echo -e "${YELLOW}Cleaning existing onnxruntime packages...${NC}"
    pip uninstall -y onnxruntime onnxruntime-rocm onnxruntime-migraphx onnxruntime-gpu 2>/dev/null || true

    # Select wheel based on ROCm version
    ROCM_ONNX_WHEEL=""
    case "$ROCM_VER" in
    7.2*)
      # ROCm 7.2: Use MIGraphX provider (best performance)
      ROCM_ONNX_WHEEL="https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/onnxruntime_migraphx-1.23.2-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
      echo -e "${BLUE}Using MIGraphX provider for ROCm 7.2${NC}"
      ;;
    7.0* | 7.1*)
      # ROCm 7.0/7.1: Use ROCm provider
      ROCM_ONNX_WHEEL="https://repo.radeon.com/rocm/manylinux/rocm-rel-7.0/onnxruntime_rocm-1.22.1-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
      echo -e "${BLUE}Using ROCm provider for ROCm 7.0/7.1${NC}"
      ;;
    *)
      echo -e "${YELLOW}Unknown ROCm version ${ROCM_VER}, trying MIGraphX wheel...${NC}"
      ROCM_ONNX_WHEEL="https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/onnxruntime_migraphx-1.23.2-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl"
      ;;
    esac

    echo -e "${BLUE}Installing onnxruntime wheel...${NC}"
    pip install "$ROCM_ONNX_WHEEL" 2>&1 | tail -3

    # Verify provider is available
    PROVIDER=$(python -c "
import onnxruntime as ort
providers = ort.get_available_providers()
if 'MIGraphXExecutionProvider' in providers:
    print('migraphx')
elif 'ROCMExecutionProvider' in providers:
    print('rocm')
else:
    print('cpu')
" 2>/dev/null)

    if [[ "$PROVIDER" == "rocm" || "$PROVIDER" == "migraphx" ]]; then
      echo -e "${GREEN}GPU support enabled: $PROVIDER${NC}"
    else
      echo -e "${YELLOW}Provider not detected (got: $PROVIDER). GPU may not work.${NC}"
    fi
  fi
# CUDA (NVIDIA GPU) - Linux only, mutually exclusive with ROCm
elif [[ "$PLATFORM" == "linux" ]] && command -v nvidia-smi &>/dev/null; then
  if nvidia-smi &>/dev/null 2>&1; then
    echo -e "${BLUE}NVIDIA GPU detected, configuring onnxruntime...${NC}"

    # Detect CUDA version from nvidia-smi or nvcc
    CUDA_VER=""
    if nvidia-smi --query-gpu=driver_version --format=csv,noheader &>/dev/null 2>&1; then
      CUDA_VER=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
    fi
    if [[ -z "$CUDA_VER" ]] && command -v nvcc &>/dev/null; then
      CUDA_VER=$(nvcc --version 2>/dev/null | grep -oE 'release [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
    fi
    echo -e "${BLUE}System CUDA version: ${CUDA_VER:-unknown}${NC}"

    # CRITICAL: Clean ALL existing onnxruntime packages first
    # Multiple onnxruntime packages cause conflicts and massive build sizes
    echo -e "${YELLOW}Cleaning existing onnxruntime packages...${NC}"
    pip uninstall -y onnxruntime onnxruntime-rocm onnxruntime-migraphx onnxruntime-gpu 2>/dev/null || true

    # onnxruntime-gpu from PyPI supports CUDA 11.8 and 12.x
    echo -e "${BLUE}Installing onnxruntime-gpu...${NC}"
    pip install onnxruntime-gpu 2>&1 | tail -3

    # Ensure onnxruntime-gpu is the active package.
    # Some transitive deps (e.g. magika via chonkie) pull in the CPU
    # onnxruntime which overwrites the GPU provider .so files.
    # Force-reinstall without deps to restore GPU providers.
    pip install --force-reinstall --no-deps onnxruntime-gpu 2>&1 | tail -3

    # Verify provider is available
    PROVIDER=$(python -c "
import onnxruntime as ort
providers = ort.get_available_providers()
if 'CUDAExecutionProvider' in providers:
    print('cuda')
else:
    print('cpu')
" 2>/dev/null)

    if [[ "$PROVIDER" == "cuda" ]]; then
      echo -e "${GREEN}GPU support enabled: CUDAExecutionProvider${NC}"
    else
      echo -e "${YELLOW}CUDAExecutionProvider not detected (got: $PROVIDER). GPU may not work.${NC}"
    fi
  fi
fi

# Inject version for build
echo -e "${BLUE}Injecting version: ${VERSION}${NC}"

# Create version file that will be read at runtime
cat >"opencode_embedder/_version.py" <<EOF
# Auto-generated at build time - do not edit
__version__ = "${VERSION}"
EOF

# Build with PyInstaller
echo -e "${BLUE}Running PyInstaller...${NC}"
pyinstaller opencode-embedder.spec --noconfirm

# Keep version file for GPU mode (Python module runs directly from venv)
# The PyInstaller binary has it bundled, but GPU mode bypasses the binary
# and runs the Python module directly, so it needs the file on disk
echo -e "${BLUE}Version file preserved for GPU mode: opencode_embedder/_version.py${NC}"

# Verify the binary (onedir mode: dist/opencode-embedder/opencode-embedder)
# Also check for macOS variant without extension
BINARY_PATH=""
if [[ -f "dist/opencode-embedder/opencode-embedder" ]]; then
  BINARY_PATH="dist/opencode-embedder/opencode-embedder"
elif [[ -f "dist/opencode-embedder/opencode-embedder.exe" ]]; then
  BINARY_PATH="dist/opencode-embedder/opencode-embedder.exe"
elif [[ -d "dist/opencode-embedder" ]]; then
  # Try to find any executable in the directory (portable for both Linux and macOS)
  for f in dist/opencode-embedder/opencode*; do
    if [[ -f "$f" && -x "$f" ]]; then
      BINARY_PATH="$f"
      break
    fi
  done
fi

# Debug: show what's in dist if binary not found
if [[ -z "$BINARY_PATH" || ! -f "$BINARY_PATH" ]]; then
  echo -e "${YELLOW}Binary not found. Contents of dist/:${NC}"
  ls -la dist/ 2>/dev/null || echo "dist/ does not exist"
  if [[ -d "dist/opencode-embedder" ]]; then
    echo -e "${YELLOW}Contents of dist/opencode-embedder/:${NC}"
    ls -la dist/opencode-embedder/ 2>/dev/null | head -20
  fi
fi

if [[ -n "$BINARY_PATH" && -f "$BINARY_PATH" ]]; then
  echo -e "${GREEN}Build successful! Binary: ${BINARY_PATH}${NC}"

  # Set LD_LIBRARY_PATH to use system ROCm libraries (not bundled ones)
  # The spec file excludes ROCm libs from bundle to avoid version mismatches
  if [[ -d "/opt/rocm/lib" ]]; then
    export LD_LIBRARY_PATH="/opt/rocm/lib:${LD_LIBRARY_PATH:-}"
  elif [[ -d "/opt/rocm-7.0.2/lib" ]]; then
    export LD_LIBRARY_PATH="/opt/rocm-7.0.2/lib:${LD_LIBRARY_PATH:-}"
  fi

  # Show version
  "$BINARY_PATH" --version

  # Verify GPU provider works (ROCm or CUDA)
  if [[ "$PLATFORM" == "linux" ]] && command -v rocm-smi &>/dev/null; then
    echo -e "${BLUE}Testing GPU provider in bundle...${NC}"
    # Run a quick provider check - the bundle should now use system ROCm libs
    GPU_CHECK=$("$BINARY_PATH" --check-gpu 2>&1 || true)
    if echo "$GPU_CHECK" | grep -q "ROCMExecutionProvider"; then
      echo -e "${GREEN}GPU provider verified: ROCMExecutionProvider${NC}"
    elif echo "$GPU_CHECK" | grep -q "failed runtime test"; then
      echo -e "${YELLOW}Warning: GPU provider failed runtime test${NC}"
      echo "$GPU_CHECK" | grep -E "failed|error" | head -3
    else
      echo -e "${YELLOW}GPU provider check inconclusive${NC}"
    fi
  elif [[ "$PLATFORM" == "linux" ]] && command -v nvidia-smi &>/dev/null; then
    echo -e "${BLUE}Testing GPU provider in bundle...${NC}"
    GPU_CHECK=$("$BINARY_PATH" --check-gpu 2>&1 || true)
    if echo "$GPU_CHECK" | grep -q "CUDAExecutionProvider"; then
      echo -e "${GREEN}GPU provider verified: CUDAExecutionProvider${NC}"
    elif echo "$GPU_CHECK" | grep -q "failed runtime test"; then
      echo -e "${YELLOW}Warning: GPU provider failed runtime test${NC}"
      echo "$GPU_CHECK" | grep -E "failed|error" | head -3
    else
      echo -e "${YELLOW}GPU provider check inconclusive${NC}"
    fi
  fi

  # Show size (total directory size)
  SIZE=$(du -sh dist/opencode-embedder | cut -f1)
  echo -e "${BLUE}Distribution size: ${SIZE}${NC}"

  # Create tarball for distribution
  DIST_DIR="../../../packages/opencode/dist/embedder"
  mkdir -p "$DIST_DIR"

  TARBALL_NAME="opencode-embedder-${PLATFORM}-${ARCH}.tar.gz"
  echo -e "${BLUE}Creating tarball: ${TARBALL_NAME}${NC}"

  # Create tarball preserving the directory structure
  (cd dist && tar -czf "../$DIST_DIR/$TARBALL_NAME" opencode-embedder)

  # Also copy the directory for local use
  rm -rf "$DIST_DIR/opencode-embedder-${PLATFORM}-${ARCH}"
  cp -r "dist/opencode-embedder" "$DIST_DIR/opencode-embedder-${PLATFORM}-${ARCH}"
  chmod +x "$DIST_DIR/opencode-embedder-${PLATFORM}-${ARCH}/opencode-embedder"

  echo -e "${GREEN}Tarball created: $DIST_DIR/$TARBALL_NAME${NC}"
  echo -e "${GREEN}Directory copied: $DIST_DIR/opencode-embedder-${PLATFORM}-${ARCH}${NC}"
else
  echo -e "${YELLOW}Build failed!${NC}"
  exit 1
fi
