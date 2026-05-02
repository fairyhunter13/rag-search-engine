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
            // GPU systems: higher concurrency since GPU handles the heavy lifting
            // The Python embedder batches requests, so more concurrency = better throughput
            match self.gpu.provider {
                GpuProvider::MIGraphX | GpuProvider::Rocm => 16,
                GpuProvider::Cuda => 16,
                GpuProvider::Metal | GpuProvider::CoreML => 8,
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
    /// This is I/O bound, so we can use more parallelism.
    pub fn scan_concurrency(&self) -> usize {
        let base = self.cpu_cores * 2;
        base.min(16).max(4) // Between 4 and 16
    }

    /// Get recommended concurrency for linked project background indexing.
    ///
    /// This controls how many linked projects (submodules, monorepo packages) can be
    /// indexed concurrently in the background during search operations.
    ///
    /// Considerations:
    /// - Each linked project indexing creates a LanceDB connection (~10-15 MB)
    /// - Embedding work goes to the Python server (which has its own limits)
    /// - Too much parallelism can cause memory pressure
    pub fn link_index_concurrency(&self) -> usize {
        if self.gpu.available {
            // GPU systems: embedding is GPU-bound, can handle more parallelism
            // But still limit to avoid memory pressure from LanceDB connections
            match self.gpu.provider {
                GpuProvider::MIGraphX | GpuProvider::Rocm | GpuProvider::Cuda => {
                    // High-end GPU: allow up to 6 concurrent link indexing
                    (self.cpu_cores / 4).clamp(2, 6)
                }
                GpuProvider::Metal | GpuProvider::CoreML => {
                    // Apple Silicon: moderate parallelism
                    (self.cpu_cores / 4).clamp(2, 4)
                }
                GpuProvider::None => self.cpu_link_index_concurrency(),
            }
        } else {
            self.cpu_link_index_concurrency()
        }
    }

    /// CPU-only link index concurrency.
    fn cpu_link_index_concurrency(&self) -> usize {
        // CPU-only: be conservative to avoid memory/CPU pressure
        // Each link index operation is memory-intensive
        let memory_limit = (self.memory_gb / 8.0).max(1.0) as usize; // ~8 GB per slot
        let core_limit = (self.cpu_cores / 4).max(1);
        memory_limit.min(core_limit).clamp(1, 4)
    }

    /// Get recommended write batch size for LanceDB.
    pub fn write_batch_size(&self) -> usize {
        if self.gpu.available {
            32 // Larger batches for GPU systems
        } else {
            16 // Smaller batches for CPU systems
        }
    }

    /// Check if this is a high-performance system (GPU available).
    pub fn is_high_performance(&self) -> bool {
        self.gpu.available
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

    #[test]
    fn test_hardware_detection() {
        let info = HardwareInfo::detect();
        println!("Hardware: {}", info.description());
        println!("Embedding concurrency: {}", info.embedding_concurrency());
        println!("Scan concurrency: {}", info.scan_concurrency());
        assert!(info.cpu_cores > 0);
        assert!(info.memory_gb > 0.0);
    }

    #[test]
    fn test_memory_usage_read() {
        let usage = MemoryUsage::read();
        // Should succeed on Linux and macOS
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        {
            let usage = usage.expect("should read memory on Linux/macOS");
            assert!(usage.total_bytes > 0, "total should be positive");
            assert!(usage.available_bytes > 0, "available should be positive");
            assert!(
                usage.available_bytes <= usage.total_bytes,
                "available should not exceed total"
            );
            println!(
                "Memory: {:.1} GB total, {:.1} GB available, {:.1}% used",
                usage.total_bytes as f64 / 1024.0 / 1024.0 / 1024.0,
                usage.available_bytes as f64 / 1024.0 / 1024.0 / 1024.0,
                usage.usage_percent(),
            );
        }
    }

    #[test]
    fn test_memory_usage_fraction() {
        let usage = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 300,
        };
        let frac = usage.usage_fraction();
        assert!((frac - 0.7).abs() < 0.001, "expected 0.7, got {}", frac);
        assert!((usage.usage_percent() - 70.0).abs() < 0.1);
    }

    #[test]
    fn test_memory_usage_exceeds() {
        let high = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 50,
        };
        assert!(high.exceeds(0.90), "95% usage should exceed 90% threshold");
        assert!(high.exceeds(0.80));

        let low = MemoryUsage {
            total_bytes: 1000,
            available_bytes: 500,
        };
        assert!(!low.exceeds(0.80), "50% usage should not exceed 80%");
        assert!(!low.exceeds(0.90));
    }

    #[test]
    fn test_memory_usage_zero_total() {
        let usage = MemoryUsage {
            total_bytes: 0,
            available_bytes: 0,
        };
        assert_eq!(usage.usage_fraction(), 0.0, "zero total should return 0");
        assert!(!usage.exceeds(0.5));
    }

    #[test]
    fn test_memory_pressure_does_not_panic() {
        // Just verify these don't panic on any platform
        let _ = memory_pressure();
        let _ = memory_critical();
    }

    #[test]
    fn test_memory_thresholds_are_ordered() {
        assert!(
            MEMORY_PRESSURE_THRESHOLD < MEMORY_CRITICAL_THRESHOLD,
            "pressure threshold should be below critical"
        );
        assert!(MEMORY_PRESSURE_THRESHOLD > 0.0 && MEMORY_PRESSURE_THRESHOLD < 1.0);
        assert!(MEMORY_CRITICAL_THRESHOLD > 0.0 && MEMORY_CRITICAL_THRESHOLD < 1.0);
    }
}
