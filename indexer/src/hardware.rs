//! Hardware detection for automatic concurrency tuning.
//!
//! Detects platform, architecture, GPU availability, and system resources
//! to determine optimal concurrency settings.

use std::path::Path;

/// Hardware capabilities detected on the current system.
#[derive(Debug, Clone)]
pub struct HardwareInfo {
    pub platform: Platform,
    pub arch: Arch,
    pub cpu_cores: usize,
    pub memory_gb: f64,
    pub gpu: GpuInfo,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Darwin,
    Windows,
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Arch {
    X64,
    Arm64,
    Unknown,
}

#[derive(Debug, Clone)]
pub struct GpuInfo {
    pub available: bool,
    pub provider: GpuProvider,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GpuProvider {
    None,
    Rocm,     // AMD GPU (Linux)
    MIGraphX, // AMD MIGraphX (Linux, better performance)
    Cuda,     // NVIDIA GPU
    Metal,    // Apple GPU (macOS)
    CoreML,   // Apple CoreML (macOS)
}

impl HardwareInfo {
    /// Detect hardware capabilities of the current system.
    pub fn detect() -> Self {
        Self {
            platform: detect_platform(),
            arch: detect_arch(),
            cpu_cores: detect_cpu_cores(),
            memory_gb: detect_memory_gb(),
            gpu: detect_gpu(),
        }
    }

    /// Get recommended embedding concurrency based on hardware.
    ///
    /// Higher concurrency for GPU systems since embedding is GPU-bound.
    /// Lower concurrency for CPU-only systems to avoid overloading.
    pub fn embedding_concurrency(&self) -> usize {
        if self.gpu.available {
            // Conservative GPU concurrency: the Python embedder has at most 1-2 workers,
            // and each worker uses ONNX Runtime with GPU acceleration. Sending more than
            // 2-3 concurrent requests just queues them in the HTTP layer, adding memory
            // pressure without throughput benefit. The embedder's own batch coalescer
            // already handles request aggregation.
            match self.gpu.provider {
                GpuProvider::MIGraphX | GpuProvider::Rocm => 3,
                GpuProvider::Cuda => 3,
                GpuProvider::Metal | GpuProvider::CoreML => 2,
                GpuProvider::None => self.cpu_embedding_concurrency(),
            }
        } else {
            self.cpu_embedding_concurrency()
        }
    }

    /// CPU-only embedding concurrency based on cores and memory.
    fn cpu_embedding_concurrency(&self) -> usize {
        // CPU embedding is memory-intensive, limit based on available RAM
        let memory_limit = (self.memory_gb / 2.0).max(1.0) as usize;
        let core_limit = (self.cpu_cores / 2).max(1);
        memory_limit.min(core_limit).min(4) // Cap at 4 for CPU-only
    }

    /// Get recommended scan concurrency (file hashing).
    ///
    /// Scan concurrency for file hashing.
    /// This is I/O bound but excessive parallelism causes CPU contention.
    /// Capped at 4 to keep background scanning lightweight.
    pub fn scan_concurrency(&self) -> usize {
        4
    }

    /// Get a human-readable description of detected hardware.
    pub fn description(&self) -> String {
        let platform = match self.platform {
            Platform::Linux => "Linux",
            Platform::Darwin => "macOS",
            Platform::Windows => "Windows",
            Platform::Unknown => "Unknown",
        };
        let arch = match self.arch {
            Arch::X64 => "x64",
            Arch::Arm64 => "arm64",
            Arch::Unknown => "unknown",
        };
        let gpu = match self.gpu.provider {
            GpuProvider::None => "None".to_string(),
            GpuProvider::Rocm => "AMD ROCm".to_string(),
            GpuProvider::MIGraphX => "AMD MIGraphX".to_string(),
            GpuProvider::Cuda => "NVIDIA CUDA".to_string(),
            GpuProvider::Metal => "Apple Metal".to_string(),
            GpuProvider::CoreML => "Apple CoreML".to_string(),
        };
        format!(
            "{}/{}, {} cores, {:.1}GB RAM, GPU: {}",
            platform, arch, self.cpu_cores, self.memory_gb, gpu
        )
    }
}

fn detect_platform() -> Platform {
    #[cfg(target_os = "linux")]
    return Platform::Linux;

    #[cfg(target_os = "macos")]
    return Platform::Darwin;

    #[cfg(target_os = "windows")]
    return Platform::Windows;

    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    return Platform::Unknown;
}

fn detect_arch() -> Arch {
    #[cfg(target_arch = "x86_64")]
    return Arch::X64;

    #[cfg(target_arch = "aarch64")]
    return Arch::Arm64;

    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    return Arch::Unknown;
}

fn detect_cpu_cores() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}

fn detect_memory_gb() -> f64 {
    #[cfg(target_os = "linux")]
    {
        // Read from /proc/meminfo
        if let Ok(content) = std::fs::read_to_string("/proc/meminfo") {
            for line in content.lines() {
                if line.starts_with("MemTotal:") {
                    let parts: Vec<&str> = line.split_whitespace().collect();
                    if parts.len() >= 2 {
                        if let Ok(kb) = parts[1].parse::<u64>() {
                            return kb as f64 / 1024.0 / 1024.0;
                        }
                    }
                }
            }
        }
    }

    #[cfg(target_os = "macos")]
    {
        // Use sysctl on macOS
        use std::process::Command;
        if let Ok(output) = Command::new("sysctl").args(["-n", "hw.memsize"]).output() {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(bytes) = s.trim().parse::<u64>() {
                    return bytes as f64 / 1024.0 / 1024.0 / 1024.0;
                }
            }
        }
    }

    // Fallback: assume 8GB
    8.0
}

fn detect_gpu() -> GpuInfo {
    // Check for ROCm (AMD GPU on Linux)
    #[cfg(target_os = "linux")]
    {
        if has_rocm() {
            // Check if MIGraphX is available (better performance)
            if has_migraphx() {
                return GpuInfo {
                    available: true,
                    provider: GpuProvider::MIGraphX,
                };
            }
            return GpuInfo {
                available: true,
                provider: GpuProvider::Rocm,
            };
        }

        // Check for CUDA (NVIDIA GPU)
        if has_cuda() {
            return GpuInfo {
                available: true,
                provider: GpuProvider::Cuda,
            };
        }
    }

    // Check for Metal/CoreML on macOS
    #[cfg(target_os = "macos")]
    {
        // Apple Silicon always has Metal/CoreML
        #[cfg(target_arch = "aarch64")]
        return GpuInfo {
            available: true,
            provider: GpuProvider::CoreML,
        };

        // Intel Mac might have Metal but no CoreML acceleration
        #[cfg(target_arch = "x86_64")]
        return GpuInfo {
            available: false,
            provider: GpuProvider::None,
        };
    }

    GpuInfo {
        available: false,
        provider: GpuProvider::None,
    }
}

#[cfg(target_os = "linux")]
fn has_rocm() -> bool {
    // Check for rocm-smi binary
    if std::process::Command::new("rocm-smi")
        .arg("--showid")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        return true;
    }

    // Fallback: check for /opt/rocm directory
    Path::new("/opt/rocm").exists()
}

#[cfg(target_os = "linux")]
fn has_migraphx() -> bool {
    // Check for MIGraphX library
    Path::new("/opt/rocm/lib/libmigraphx.so").exists()
        || Path::new("/opt/rocm/lib64/libmigraphx.so").exists()
}

#[cfg(target_os = "linux")]
fn has_cuda() -> bool {
    // Check for nvidia-smi binary
    if std::process::Command::new("nvidia-smi")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        return true;
    }

    // Fallback: check for CUDA directory
    Path::new("/usr/local/cuda").exists()
}

/// System memory usage snapshot.
#[derive(Debug, Clone, Copy)]
pub struct MemoryUsage {
    pub total_bytes: u64,
    pub available_bytes: u64,
}

impl MemoryUsage {
    /// Read current memory usage from the OS.
    /// Returns None if unable to read (e.g., unsupported platform).
    pub fn read() -> Option<Self> {
        read_memory_usage()
    }

    /// Memory usage as a fraction (0.0 - 1.0).
    pub fn usage_fraction(&self) -> f64 {
        if self.total_bytes == 0 {
            return 0.0;
        }
        let used = self.total_bytes.saturating_sub(self.available_bytes);
        used as f64 / self.total_bytes as f64
    }

    /// Memory usage as a percentage (0 - 100).
pub fn usage_percent(&self) -> f64 {
    self.usage_fraction() * 100.0
}

/// Whether memory usage exceeds the given threshold (0.0 - 1.0).
    pub fn exceeds(&self, threshold: f64) -> bool {
        self.usage_fraction() > threshold
    }
}

/// Default memory pressure threshold: if system memory usage exceeds this,
/// the pipeline applies backpressure (blocks non-blocking writes, reduces buffers).
/// 80% leaves headroom for OS caches, other processes, and LanceDB internals.
pub const MEMORY_PRESSURE_THRESHOLD: f64 = 0.75;

/// Critical memory threshold: if exceeded, the pipeline aggressively flushes.
pub const MEMORY_CRITICAL_THRESHOLD: f64 = 0.85;

/// Check if system is under memory pressure.
/// Returns true if memory usage exceeds MEMORY_PRESSURE_THRESHOLD.
pub fn memory_pressure() -> bool {
    MemoryUsage::read()
        .map(|m| m.exceeds(MEMORY_PRESSURE_THRESHOLD))
        .unwrap_or(false)
}

/// Check if system is at critical memory level.
pub fn memory_critical() -> bool {
    MemoryUsage::read()
        .map(|m| m.exceeds(MEMORY_CRITICAL_THRESHOLD))
        .unwrap_or(false)
}

#[cfg(target_os = "linux")]
fn read_memory_usage() -> Option<MemoryUsage> {
    let content = std::fs::read_to_string("/proc/meminfo").ok()?;
    let mut total: Option<u64> = None;
    let mut available: Option<u64> = None;

    for line in content.lines() {
        if line.starts_with("MemTotal:") {
            total = line
                .split_whitespace()
                .nth(1)
                .and_then(|v| v.parse::<u64>().ok());
        } else if line.starts_with("MemAvailable:") {
            available = line
                .split_whitespace()
                .nth(1)
                .and_then(|v| v.parse::<u64>().ok());
        }
        if total.is_some() && available.is_some() {
            break;
        }
    }

    Some(MemoryUsage {
        total_bytes: total? * 1024, // /proc/meminfo reports in kB
        available_bytes: available? * 1024,
    })
}

#[cfg(target_os = "macos")]
fn read_memory_usage() -> Option<MemoryUsage> {
    use std::process::Command;

    // Total memory from sysctl
    let total = Command::new("sysctl")
        .args(["-n", "hw.memsize"])
        .output()
        .ok()
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .and_then(|s| s.trim().parse::<u64>().ok())?;

    // vm_stat gives us page-level stats
    let output = Command::new("vm_stat").output().ok()?;
    let text = String::from_utf8(output.stdout).ok()?;

    let mut free: u64 = 0;
    let mut inactive: u64 = 0;
    let mut speculative: u64 = 0;
    let page_size: u64 = 16384; // macOS default on Apple Silicon; 4096 on Intel

    // Parse actual page size from the first line
    let actual_page_size = text
        .lines()
        .next()
        .and_then(|line| {
            // "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
            line.split("page size of ")
                .nth(1)
                .and_then(|s| s.split(' ').next())
                .and_then(|s| s.parse::<u64>().ok())
        })
        .unwrap_or(page_size);

    for line in text.lines() {
        let parts: Vec<&str> = line.split(':').collect();
        if parts.len() < 2 {
            continue;
        }
        let key = parts[0].trim();
        let val = parts[1].trim().trim_end_matches('.');
        let pages = val.parse::<u64>().unwrap_or(0);

        match key {
            "Pages free" => free = pages,
            "Pages inactive" => inactive = pages,
            "Pages speculative" => speculative = pages,
            _ => {}
        }
    }

    // Available ≈ free + inactive + speculative (similar to Linux MemAvailable)
    let available = (free + inactive + speculative) * actual_page_size;

    Some(MemoryUsage {
        total_bytes: total,
        available_bytes: available,
    })
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn read_memory_usage() -> Option<MemoryUsage> {
    None // Unsupported platform
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_hw(
        platform: Platform,
        arch: Arch,
        cores: usize,
        mem_gb: f64,
        gpu: GpuProvider,
    ) -> HardwareInfo {
        HardwareInfo {
            platform,
            arch,
            cpu_cores: cores,
            memory_gb: mem_gb,
            gpu: GpuInfo {
                available: gpu != GpuProvider::None,
                provider: gpu,
            },
        }
    }

    // --- HardwareInfo::embedding_concurrency ---

    #[test]
    fn embedding_concurrency_cuda_returns_3() {
        let hw = make_hw(Platform::Linux, Arch::X64, 16, 32.0, GpuProvider::Cuda);
        assert_eq!(hw.embedding_concurrency(), 3);
    }

    #[test]
    fn embedding_concurrency_rocm_returns_3() {
        let hw = make_hw(Platform::Linux, Arch::X64, 16, 32.0, GpuProvider::Rocm);
        assert_eq!(hw.embedding_concurrency(), 3);
    }

    #[test]
    fn embedding_concurrency_metal_returns_2_or_3() {
        let hw = make_hw(Platform::Darwin, Arch::Arm64, 16, 32.0, GpuProvider::Metal);
        assert_eq!(hw.embedding_concurrency(), 2);
    }

    #[test]
    fn embedding_concurrency_no_gpu_falls_to_cpu() {
        let hw = make_hw(Platform::Linux, Arch::X64, 4, 2.0, GpuProvider::None);
        // cpu_embedding_concurrency: memory_limit = max(2/2, 1) = 1,
        // core_limit = max(4/2, 1) = 2 → min(1, 2, 4) = 1
        assert_eq!(hw.embedding_concurrency(), 1);
    }

    // --- HardwareInfo::cpu_embedding_concurrency (via embedding_concurrency with no GPU) ---

    #[test]
    fn cpu_concurrency_capped_at_4() {
        let hw = make_hw(Platform::Linux, Arch::X64, 64, 256.0, GpuProvider::None);
        // memory_limit = max(256/2, 1) = 128, core_limit = max(64/2, 1) = 32
        // min(128, 32, 4) = 4
        assert_eq!(hw.embedding_concurrency(), 4);
    }

    #[test]
    fn cpu_concurrency_low_memory_returns_1() {
        let hw = make_hw(Platform::Linux, Arch::X64, 64, 1.0, GpuProvider::None);
        // memory_limit = max(1/2, 1) = 1, core_limit = max(64/2, 1) = 32
        // min(1, 32, 4) = 1
        assert_eq!(hw.embedding_concurrency(), 1);
    }

    #[test]
    fn cpu_concurrency_low_cores_returns_1() {
        let hw = make_hw(Platform::Linux, Arch::X64, 2, 16.0, GpuProvider::None);
        // memory_limit = max(16/2, 1) = 8, core_limit = max(2/2, 1) = 1
        // min(8, 1, 4) = 1
        assert_eq!(hw.embedding_concurrency(), 1);
    }

    #[test]
    fn cpu_concurrency_minimum_is_1() {
        let hw = make_hw(Platform::Linux, Arch::X64, 0, 0.0, GpuProvider::None);
        assert!(hw.embedding_concurrency() >= 1);
    }

    // --- HardwareInfo::scan_concurrency ---

    #[test]
    fn scan_concurrency_always_4() {
        let hw_weak = make_hw(Platform::Linux, Arch::X64, 2, 0.5, GpuProvider::None);
        assert_eq!(hw_weak.scan_concurrency(), 4);

        let hw_beefy = make_hw(
            Platform::Darwin,
            Arch::Arm64,
            128,
            1024.0,
            GpuProvider::Metal,
        );
        assert_eq!(hw_beefy.scan_concurrency(), 4);
    }

    // --- HardwareInfo::description ---

    #[test]
    fn description_contains_platform() {
        let hw = make_hw(Platform::Linux, Arch::X64, 4, 8.0, GpuProvider::None);
        assert!(hw.description().contains("Linux"));

        let hw = make_hw(Platform::Darwin, Arch::Arm64, 4, 8.0, GpuProvider::Metal);
        assert!(hw.description().contains("macOS"));
    }

    #[test]
    fn description_contains_cuda() {
        let hw = make_hw(Platform::Linux, Arch::X64, 16, 32.0, GpuProvider::Cuda);
        assert!(hw.description().contains("NVIDIA CUDA"));
    }

    #[test]
    fn description_contains_no_gpu() {
        let hw = make_hw(Platform::Linux, Arch::X64, 4, 8.0, GpuProvider::None);
        assert!(hw.description().contains("None"));
    }

    // --- MemoryUsage ---

    #[test]
    fn usage_fraction_zero_total() {
        let mem = MemoryUsage {
            total_bytes: 0,
            available_bytes: 100,
        };
        assert!((mem.usage_fraction() - 0.0).abs() < 1e-6);
    }

    #[test]
    fn usage_fraction_half_used() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 500,
        };
        assert!((mem.usage_fraction() - 0.5).abs() < 1e-6);
    }

    #[test]
    fn usage_fraction_fully_used() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 0,
        };
        assert!((mem.usage_fraction() - 1.0).abs() < 1e-6);
    }

    #[test]
    fn usage_fraction_none_used() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 1000,
        };
        assert!((mem.usage_fraction() - 0.0).abs() < 1e-6);
    }

    #[test]
    fn usage_fraction_negative_available() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 2000,
        };
        // saturating_sub ensures no underflow → used = 0 → fraction = 0.0
        assert!((mem.usage_fraction() - 0.0).abs() < 1e-6);
    }

    #[test]
    fn usage_percent_returns_0_to_100() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 750,
        };
        // usage_fraction = 250/1000 = 0.25 → 25.0%
        assert!((mem.usage_percent() - 25.0).abs() < 1e-6);
    }

    #[test]
    fn exceeds_below_threshold_false() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 800,
        };
        // usage_fraction = 0.2 → 0.2 > 0.5 = false
        assert!(!mem.exceeds(0.5));
    }

    #[test]
    fn exceeds_above_threshold_true() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 100,
        };
        // usage_fraction = 0.9 → 0.9 > 0.5 = true
        assert!(mem.exceeds(0.5));
    }

    #[test]
    fn exceeds_exactly_at_threshold() {
        let mem = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 500,
        };
        // usage_fraction = 0.5, exceeds uses strict > → 0.5 > 0.5 = false
        assert!(!mem.exceeds(0.5));
    }

    #[test]
    fn memory_pressure_threshold_is_0_75() {
        assert_eq!(MEMORY_PRESSURE_THRESHOLD, 0.75);
    }

    #[test]
    fn memory_critical_threshold_is_0_85() {
        assert_eq!(MEMORY_CRITICAL_THRESHOLD, 0.85);
    }

    // --- detect functions ---

    #[test]
    fn detect_platform_matches_compile_target() {
        let platform = detect_platform();
        if cfg!(target_os = "linux") {
            assert_eq!(platform, Platform::Linux);
        } else if cfg!(target_os = "macos") {
            assert_eq!(platform, Platform::Darwin);
        } else if cfg!(target_os = "windows") {
            assert_eq!(platform, Platform::Windows);
        } else {
            assert_eq!(platform, Platform::Unknown);
        }
    }

    #[test]
    fn detect_arch_matches_compile_target() {
        let arch = detect_arch();
        if cfg!(target_arch = "x86_64") {
            assert_eq!(arch, Arch::X64);
        } else if cfg!(target_arch = "aarch64") {
            assert_eq!(arch, Arch::Arm64);
        } else {
            assert_eq!(arch, Arch::Unknown);
        }
    }
}
