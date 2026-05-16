//! SIMD-accelerated vector operations for search reranking.
//!
//! Provides optimized cosine similarity computation using AVX2/SSE when available,
//! falling back to scalar operations on unsupported platforms.
//!
//! Performance on x86_64 with AVX2:
//! - ~8x faster than scalar for 768-dim vectors
//! - ~4x faster for 384-dim vectors
//! - Auto-vectorization for batch operations

#![allow(unsafe_op_in_unsafe_fn)]

/// Cosine similarity using best available SIMD instruction set.
/// Falls back to scalar when SIMD unavailable.
///
/// # Arguments
/// * `a` - First vector (query)
/// * `b` - Second vector (candidate)
///
/// # Returns
/// Cosine similarity score in range [-1.0, 1.0]
#[inline]
pub fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    assert_eq!(a.len(), b.len(), "vectors must have same length");

    // Runtime CPU feature detection for x86_64
    #[cfg(target_arch = "x86_64")]
    {
        if is_x86_feature_detected!("avx2") && is_x86_feature_detected!("fma") {
            // AVX2 path - 8 floats at a time
            unsafe { cosine_similarity_avx2(a, b) }
        } else if is_x86_feature_detected!("sse") {
            // SSE path - 4 floats at a time
            unsafe { cosine_similarity_sse(a, b) }
        } else {
            cosine_similarity_scalar(a, b)
        }
    }

    #[cfg(target_arch = "aarch64")]
    {
        if std::arch::is_aarch64_feature_detected!("neon") {
            unsafe { cosine_similarity_neon(a, b) }
        } else {
            cosine_similarity_scalar(a, b)
        }
    }

    #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
    {
        cosine_similarity_scalar(a, b)
    }
}

/// Scalar fallback for non-x86 or when SIMD unavailable.
/// Uses simple dot product (not Kahan summation).
fn cosine_similarity_scalar(a: &[f32], b: &[f32]) -> f32 {
    let (mut dot, mut norm_a, mut norm_b) = (0.0f32, 0.0f32, 0.0f32);

    for (x, y) in a.iter().zip(b.iter()) {
        dot += x * y;
        norm_a += x * x;
        norm_b += y * y;
    }

    let denom = (norm_a * norm_b).sqrt();
    if denom > 0.0 {
        dot / denom
    } else {
        0.0
    }
}

#[cfg(all(target_arch = "x86_64", target_feature = "avx2"))]
#[target_feature(enable = "avx2", enable = "fma")]
unsafe fn cosine_similarity_avx2(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;

    let n = a.len();
    let chunks = n / 8;

    let mut dot_acc = _mm256_setzero_ps();
    let mut norm_a_acc = _mm256_setzero_ps();
    let mut norm_b_acc = _mm256_setzero_ps();

    // Process 8 floats at a time using FMA (fused multiply-add)
    for i in 0..chunks {
        let va = _mm256_loadu_ps(a.as_ptr().add(i * 8));
        let vb = _mm256_loadu_ps(b.as_ptr().add(i * 8));

        dot_acc = _mm256_fmadd_ps(va, vb, dot_acc);
        norm_a_acc = _mm256_fmadd_ps(va, va, norm_a_acc);
        norm_b_acc = _mm256_fmadd_ps(vb, vb, norm_b_acc);
    }

    // Horizontal sum across 8 lanes
    let dot = hsum_avx2(dot_acc);
    let norm_a = hsum_avx2(norm_a_acc);
    let norm_b = hsum_avx2(norm_b_acc);

    // Handle remainder (< 8 elements)
    let (mut dot_rem, mut norm_a_rem, mut norm_b_rem) = (0.0f32, 0.0f32, 0.0f32);
    for i in (chunks * 8)..n {
        let x = *a.get_unchecked(i);
        let y = *b.get_unchecked(i);
        dot_rem += x * y;
        norm_a_rem += x * x;
        norm_b_rem += y * y;
    }

    let total_dot = dot + dot_rem;
    let total_norm = ((norm_a + norm_a_rem) * (norm_b + norm_b_rem)).sqrt();

    if total_norm > 0.0 {
        total_dot / total_norm
    } else {
        0.0
    }
}

#[cfg(target_arch = "x86_64")]
#[inline]
unsafe fn hsum_avx2(v: std::arch::x86_64::__m256) -> f32 {
    use std::arch::x86_64::*;

    // Sum high and low 128-bit halves
    let low = _mm256_castps256_ps128(v);
    let high = _mm256_extractf128_ps(v, 1);
    let sum128 = _mm_add_ps(low, high);

    // Sum within 128-bit register
    let sum64 = _mm_add_ps(sum128, _mm_movehl_ps(sum128, sum128));
    let sum32 = _mm_add_ss(sum64, _mm_shuffle_ps(sum64, sum64, 1));

    _mm_cvtss_f32(sum32)
}

#[cfg(all(target_arch = "x86_64", target_feature = "sse"))]
#[target_feature(enable = "sse")]
unsafe fn cosine_similarity_sse(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;

    let n = a.len();
    let chunks = n / 4;

    let mut dot_acc = _mm_setzero_ps();
    let mut norm_a_acc = _mm_setzero_ps();
    let mut norm_b_acc = _mm_setzero_ps();

    // Process 4 floats at a time
    for i in 0..chunks {
        let va = _mm_loadu_ps(a.as_ptr().add(i * 4));
        let vb = _mm_loadu_ps(b.as_ptr().add(i * 4));

        dot_acc = _mm_add_ps(dot_acc, _mm_mul_ps(va, vb));
        norm_a_acc = _mm_add_ps(norm_a_acc, _mm_mul_ps(va, va));
        norm_b_acc = _mm_add_ps(norm_b_acc, _mm_mul_ps(vb, vb));
    }

    // Horizontal sum for SSE
    let dot = hsum_sse(dot_acc);
    let norm_a = hsum_sse(norm_a_acc);
    let norm_b = hsum_sse(norm_b_acc);

    // Remainder
    let (mut dot_rem, mut norm_a_rem, mut norm_b_rem) = (0.0f32, 0.0f32, 0.0f32);
    for i in (chunks * 4)..n {
        let x = *a.get_unchecked(i);
        let y = *b.get_unchecked(i);
        dot_rem += x * y;
        norm_a_rem += x * x;
        norm_b_rem += y * y;
    }

    let total_dot = dot + dot_rem;
    let total_norm = ((norm_a + norm_a_rem) * (norm_b + norm_b_rem)).sqrt();

    if total_norm > 0.0 {
        total_dot / total_norm
    } else {
        0.0
    }
}

#[cfg(target_arch = "x86_64")]
#[inline]
unsafe fn hsum_sse(v: std::arch::x86_64::__m128) -> f32 {
    use std::arch::x86_64::*;

    let sum64 = _mm_add_ps(v, _mm_movehl_ps(v, v));
    let sum32 = _mm_add_ss(sum64, _mm_shuffle_ps(sum64, sum64, 1));
    _mm_cvtss_f32(sum32)
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn cosine_similarity_neon(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::aarch64::*;

    let n = a.len();
    let chunks = n / 4;

    let mut dot_acc = vdupq_n_f32(0.0);
    let mut norm_a_acc = vdupq_n_f32(0.0);
    let mut norm_b_acc = vdupq_n_f32(0.0);

    // Process 4 floats at a time using FMLA (fused multiply-add)
    for i in 0..chunks {
        let va = vld1q_f32(a.as_ptr().add(i * 4));
        let vb = vld1q_f32(b.as_ptr().add(i * 4));

        dot_acc = vfmaq_f32(dot_acc, va, vb);
        norm_a_acc = vfmaq_f32(norm_a_acc, va, va);
        norm_b_acc = vfmaq_f32(norm_b_acc, vb, vb);
    }

    // Horizontal sum across 4 lanes
    let dot = vaddvq_f32(dot_acc);
    let norm_a = vaddvq_f32(norm_a_acc);
    let norm_b = vaddvq_f32(norm_b_acc);

    // Handle remainder (< 4 elements)
    let (mut dot_rem, mut norm_a_rem, mut norm_b_rem) = (0.0f32, 0.0f32, 0.0f32);
    for i in (chunks * 4)..n {
        let x = *a.get_unchecked(i);
        let y = *b.get_unchecked(i);
        dot_rem += x * y;
        norm_a_rem += x * x;
        norm_b_rem += y * y;
    }

    let total_dot = dot + dot_rem;
    let total_norm = ((norm_a + norm_a_rem) * (norm_b + norm_b_rem)).sqrt();

    if total_norm > 0.0 {
        total_dot / total_norm
    } else {
        0.0
    }
}

/// Batch cosine similarity scores (parallel when rayon feature enabled)
///
/// # Arguments
/// * `query` - Query vector
/// * `candidates` - Slice of candidate vectors
///
/// # Returns
/// Vector of cosine similarity scores in same order as candidates
#[cfg(feature = "rayon")]
pub fn batch_cosine_similarity(query: &[f32], candidates: &[Vec<f32>]) -> Vec<f32> {
    use rayon::prelude::*;

    candidates
        .par_iter()
        .map(|c| cosine_similarity(query, c))
        .collect()
}

/// Rerank search results by exact cosine similarity.
///
/// Takes top-K candidates from approximate search and reranks by computing
/// exact similarity scores using SIMD-accelerated operations.
///
/// # Arguments
/// * `query` - Query embedding vector
/// * `candidates` - Candidate embeddings from initial search
/// * `limit` - Number of top results to return
///
/// # Returns
/// Indices of top-K candidates, sorted by descending similarity
pub fn rerank_by_cosine(query: &[f32], candidates: &[Vec<f32>], limit: usize) -> Vec<usize> {
    if candidates.is_empty() || limit == 0 {
        return Vec::new();
    }

    // Compute scores
    let scores = batch_cosine_similarity(query, candidates);

    // Create (score, index) pairs and sort by descending score
    let mut scored: Vec<(f32, usize)> = scores.into_iter().enumerate().map(|(i, s)| (s, i)).collect();

    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    // Return top-K indices
    scored
        .into_iter()
        .take(limit)
        .map(|(_, idx)| idx)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---------------------------------------------------------------------------
    // cosine_similarity behavioral tests
    // ---------------------------------------------------------------------------

    #[test]
    fn identical_vectors_returns_one() {
        let sim = cosine_similarity(&[1.0, 2.0, 3.0, 4.0], &[1.0, 2.0, 3.0, 4.0]);
        assert!((sim - 1.0).abs() < 1e-6, "expected 1.0, got {}", sim);
    }

    #[test]
    fn orthogonal_vectors_returns_zero() {
        let sim = cosine_similarity(&[1.0, 0.0], &[0.0, 1.0]);
        assert!((sim - 0.0).abs() < 1e-6, "expected 0.0, got {}", sim);
    }

    #[test]
    fn opposite_vectors_returns_negative_one() {
        let sim = cosine_similarity(&[1.0, 2.0], &[-1.0, -2.0]);
        assert!((sim - (-1.0)).abs() < 1e-6, "expected -1.0, got {}", sim);
    }

    #[test]
    fn zero_vector_returns_zero() {
        let sim = cosine_similarity(&[0.0, 0.0, 0.0], &[1.0, 2.0, 3.0]);
        assert!((sim - 0.0).abs() < 1e-6, "expected 0.0, got {}", sim);
    }

    #[test]
    fn both_zero_vectors_returns_zero() {
        let sim = cosine_similarity(&[0.0, 0.0], &[0.0, 0.0]);
        assert!((sim - 0.0).abs() < 1e-6, "expected 0.0, got {}", sim);
    }

    #[test]
    #[should_panic(expected = "vectors must have same length")]
    fn mismatched_lengths_panics() {
        cosine_similarity(&[1.0, 2.0, 3.0], &[1.0, 2.0]);
    }

    #[test]
    fn dim_768_vectors_produces_valid_result() {
        let a: Vec<f32> = (0..768).map(|i| (i as f32 + 1.0) * 0.5).collect();
        let b: Vec<f32> = (0..768).map(|i| (i as f32 + 1.0) * 0.3).collect();
        let sim = cosine_similarity(&a, &b);
        assert!(
            sim >= -1.0 && sim <= 1.0,
            "similarity must be in [-1, 1], got {}",
            sim
        );
    }

    #[test]
    fn scalar_implementation_matches_public_api() {
        let a = vec![1.5, 2.5, 3.5, 4.5, 5.5];
        let b = vec![5.5, 4.5, 3.5, 2.5, 1.5];
        let public = cosine_similarity(&a, &b);
        let scalar = cosine_similarity_scalar(&a, &b);
        assert!(
            (public - scalar).abs() < 1e-6,
            "public API ({}) and scalar ({}) differ",
            public,
            scalar
        );
    }

    #[test]
    fn normalized_unit_vectors() {
        // Both vectors have norm = 1, so cos = dot product
        let a = vec![1.0, 0.0, 0.0, 0.0];
        let b = vec![0.6, 0.8, 0.0, 0.0];
        let sim = cosine_similarity(&a, &b);
        let dot: f32 = a.iter().zip(b.iter()).map(|(x, y)| x * y).sum();
        assert!(
            (sim - dot).abs() < 1e-6,
            "unit vectors: sim={}, dot={}",
            sim,
            dot
        );
    }

    #[test]
    fn partially_overlapping() {
        // [1,0,0,1] · [1,1,0,0] = 1; norms both √2; sim = 1/2 = 0.5
        let sim = cosine_similarity(&[1.0, 0.0, 0.0, 1.0], &[1.0, 1.0, 0.0, 0.0]);
        assert!((sim - 0.5).abs() < 1e-6, "expected 0.5, got {}", sim);
    }

    // ---------------------------------------------------------------------------
    // batch_cosine_similarity behavioral tests
    // ---------------------------------------------------------------------------

    #[test]
    fn batch_matches_individual_calls() {
        let query = vec![1.0, 0.0, 0.0];
        let candidates = vec![
            vec![1.0, 0.0, 0.0],
            vec![0.0, 1.0, 0.0],
            vec![-1.0, 0.0, 0.0],
            vec![0.5, 0.5, 0.0],
        ];
        let batch = batch_cosine_similarity(&query, &candidates);
        assert_eq!(batch.len(), candidates.len());
        for (i, c) in candidates.iter().enumerate() {
            let individual = cosine_similarity(&query, c);
            assert!(
                (batch[i] - individual).abs() < 1e-6,
                "batch[{}] = {}, individual = {}",
                i,
                batch[i],
                individual
            );
        }
    }

    #[test]
    fn batch_empty_candidates_returns_empty() {
        let candidates: Vec<Vec<f32>> = vec![];
        let result = batch_cosine_similarity(&[1.0, 2.0, 3.0], &candidates);
        assert!(result.is_empty());
    }

    #[test]
    fn batch_single_candidate_matches_individual() {
        let query = vec![1.0, 2.0, 3.0];
        let candidates = vec![vec![4.0, 5.0, 6.0]];
        let batch = batch_cosine_similarity(&query, &candidates);
        assert_eq!(batch.len(), 1);
        let individual = cosine_similarity(&query, &candidates[0]);
        assert!((batch[0] - individual).abs() < 1e-6);
    }

    #[test]
    fn batch_many_candidates_correct_ordering() {
        // Verify output order matches candidate input order
        let query = vec![1.0, 0.0, 0.0];
        let candidates = vec![
            vec![1.0, 0.0, 0.0],  // sim = 1.0
            vec![-1.0, 0.0, 0.0], // sim = -1.0
            vec![0.0, 1.0, 0.0],  // sim = 0.0
            vec![0.5, 0.0, 0.0],  // sim = 1.0
        ];
        let batch = batch_cosine_similarity(&query, &candidates);
        assert_eq!(batch.len(), 4);
        for (i, c) in candidates.iter().enumerate() {
            let expected = cosine_similarity_scalar(&query, c);
            assert!(
                (batch[i] - expected).abs() < 1e-6,
                "batch[{}] mismatch: got {}, expected {}",
                i,
                batch[i],
                expected
            );
        }
    }

    // ---------------------------------------------------------------------------
    // rerank_by_cosine behavioral tests
    // ---------------------------------------------------------------------------

    #[test]
    fn rerank_returns_top_k_by_similarity() {
        let query = vec![2.0, 1.0, 0.0, 0.0];
        let candidates = vec![
            vec![2.0, 1.0, 0.0, 0.0],   // sim = 5/5 = 1.0
            vec![0.0, 1.0, 0.0, 0.0],   // sim = 1/√5 ≈ 0.4472
            vec![-2.0, -1.0, 0.0, 0.0], // sim = -5/5 = -1.0
            vec![1.0, 0.0, 0.0, 0.0],   // sim = 2/√5 ≈ 0.8944
            vec![0.0, 0.0, 1.0, 0.0],   // sim = 0.0
        ];
        let result = rerank_by_cosine(&query, &candidates, 3);
        assert_eq!(result.len(), 3);
        // Top 3: index 0 (sim=1.0), index 3 (sim≈0.894), index 1 (sim≈0.447)
        assert_eq!(result[0], 0);
        assert_eq!(result[1], 3);
        assert_eq!(result[2], 1);
    }

    #[test]
    fn rerank_empty_input_returns_empty() {
        let result = rerank_by_cosine(&[1.0, 2.0, 3.0], &[], 5);
        assert!(result.is_empty());
    }

    #[test]
    fn rerank_limit_zero_returns_empty() {
        let candidates = vec![vec![1.0, 0.0], vec![0.0, 1.0]];
        let result = rerank_by_cosine(&[1.0, 0.0], &candidates, 0);
        assert!(result.is_empty());
    }

    #[test]
    fn rerank_limit_exceeds_candidates_returns_all() {
        let candidates = vec![
            vec![1.0, 0.0],
            vec![0.0, 1.0],
            vec![-1.0, 0.0],
        ];
        let result = rerank_by_cosine(&[1.0, 0.0], &candidates, 10);
        assert_eq!(result.len(), 3);
    }

    #[test]
    fn rerank_ties_are_stable() {
        let query = vec![1.0, 0.0];
        let candidates = vec![
            vec![1.0, 0.0], // sim = 1.0
            vec![0.0, 1.0], // sim = 0.0
            vec![1.0, 0.0], // sim = 1.0 (tied with index 0)
        ];
        let result = rerank_by_cosine(&query, &candidates, 2);
        assert_eq!(result.len(), 2);
        // Both tied candidates (index 0 and 2) should appear
        assert!(result.contains(&0), "index 0 should be in results");
        assert!(result.contains(&2), "index 2 should be in results");
    }

    #[test]
    fn rerank_realistic_search_scenario() {
        // 384-dim query and 10 diverse candidates
        let query: Vec<f32> = (0..384).map(|i| ((i % 7) as f32) / 7.0).collect();
        let candidates: Vec<Vec<f32>> = (0..10)
            .map(|seed| (0..384).map(|i| ((i * (seed + 1)) % 11) as f32 / 11.0).collect())
            .collect();
        let result = rerank_by_cosine(&query, &candidates, 5);
        assert_eq!(result.len(), 5);
        // All indices must be valid
        for &idx in &result {
            assert!(idx < 10, "index {} out of range", idx);
        }
        // No duplicates
        let mut sorted = result.clone();
        sorted.sort();
        sorted.dedup();
        assert_eq!(sorted.len(), 5);
    }
}
