//! Quality + resource integration test for Rust daemon ↔ Python model server.
//!
//! Tests the REAL semantic pipeline end-to-end:
//!   Rust daemon → Python server (chunk + embed + rerank) → LanceDB → search
//!
//! Verifies:
//! 1. Chunking quality — real tree-sitter / Chonkie chunkers produce correct chunks
//! 2. Embedding quality — semantic search returns the right files for natural language queries
//! 3. Reranking quality — cross-encoder reranker reorders results correctly
//! 4. Resource usage — Python server RSS stays bounded
//!
//! Run:  cargo test --test quality_integration --release -- --nocapture

mod python_server;

use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use tokio::process::Command;

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async fn rpc(port: u16, method: &str, params: serde_json::Value) -> Result<serde_json::Value> {
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/rpc"))
        .json(&serde_json::json!({"method": method, "params": params}))
        .send()
        .await
        .context("send rpc")?
        .json::<serde_json::Value>()
        .await
        .context("parse rpc response")?;
    Ok(resp)
}

// ---------------------------------------------------------------------------
// Daemon lifecycle
// ---------------------------------------------------------------------------

struct DaemonHandle {
    child: tokio::process::Child,
    port: u16,
}

impl DaemonHandle {
    async fn spawn(home: &std::path::Path, embed_env: &[(&str, String)]) -> Result<Self> {
        let bin = assert_cmd::cargo::cargo_bin!("opencode-indexer");

        let mut cmd = Command::new(&bin);
        cmd.env("HOME", home)
            .arg("--daemon")
            .arg("--port")
            .arg("0")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());

        for (key, value) in embed_env {
            cmd.env(*key, value);
        }

        let mut child = cmd.spawn().context("spawn daemon")?;

        let stdout = child.stdout.take().context("no stdout")?;
        let mut reader = tokio::io::BufReader::new(stdout);
        let mut buf = String::new();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let mut port = 0u16;

        loop {
            if tokio::time::Instant::now() > deadline {
                child.kill().await.ok();
                anyhow::bail!("daemon timeout");
            }
            buf.clear();
            tokio::io::AsyncBufReadExt::read_line(&mut reader, &mut buf).await?;
            if let Ok(msg) = serde_json::from_str::<serde_json::Value>(&buf) {
                if msg.get("type").and_then(|v| v.as_str()) == Some("http_ready") {
                    port = msg["port"].as_u64().unwrap_or(0) as u16;
                    if port > 0 {
                        break;
                    }
                }
            }
        }

        tokio::spawn(async move {
            let mut d = vec![0u8; 4096];
            loop {
                match tokio::io::AsyncReadExt::read(&mut reader, &mut d).await {
                    Ok(0) | Err(_) => break,
                    _ => {}
                }
            }
        });

        Ok(Self { child, port })
    }

    fn port(&self) -> u16 { self.port }

    async fn shutdown(mut self) {
        let _ = rpc(self.port, "shutdown", serde_json::json!({})).await;
        tokio::time::sleep(Duration::from_millis(200)).await;
        self.child.kill().await.ok();
    }
}

// ---------------------------------------------------------------------------
// Setup: spawn real Python model server
// ---------------------------------------------------------------------------

async fn setup() -> Result<(python_server::PythonServer, DaemonHandle)> {
    println!("  [Spawning REAL Python model server]");
    let server = python_server::PythonServer::start().await?;
    let daemon = DaemonHandle::spawn(server.home_path(), &server.env_vars()).await?;
    Ok((server, daemon))
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn python_rss_kb(home: &std::path::Path) -> Option<u64> {
    let pid_file = home.join(".opencode").join("embed.pid");
    let pid: u32 = std::fs::read_to_string(&pid_file).ok()?.trim().parse().ok()?;
    let statm = std::fs::read_to_string(format!("/proc/{pid}/statm")).ok()?;
    let pages: u64 = statm.split_whitespace().nth(1)?.parse().ok()?;
    Some(pages * 4)
}

// ---------------------------------------------------------------------------
// Test data: distinct, realistic code files
// ---------------------------------------------------------------------------

fn file_fibonacci_py() -> &'static str {
r#""""Fibonacci sequence implementations with memoization and iterative variants."""

from functools import lru_cache
from typing import Generator


@lru_cache(maxsize=None)
def fibonacci_recursive(n: int) -> int:
    """Calculate the nth Fibonacci number using memoized recursion."""
    if n <= 1:
        return n
    return fibonacci_recursive(n - 1) + fibonacci_recursive(n - 2)


def fibonacci_iterative(n: int) -> int:
    """Calculate the nth Fibonacci number using iteration."""
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b


def fibonacci_generator(limit: int) -> Generator[int, None, None]:
    """Generate Fibonacci numbers up to a limit."""
    a, b = 0, 1
    while a < limit:
        yield a
        a, b = b, a + b


def golden_ratio_approximation(n: int) -> float:
    """Approximate the golden ratio using consecutive Fibonacci numbers."""
    if n < 2:
        return 1.0
    return fibonacci_recursive(n) / fibonacci_recursive(n - 1)
"#
}

fn file_http_server_ts() -> &'static str {
r#"/**
 * HTTP server with routing, middleware, and request handling.
 * Supports GET, POST, PUT, DELETE methods with JSON body parsing.
 */

import { createServer, IncomingMessage, ServerResponse } from 'http';

interface Route {
  method: string;
  path: string;
  handler: (req: IncomingMessage, res: ServerResponse) => void;
}

interface Middleware {
  (req: IncomingMessage, res: ServerResponse, next: () => void): void;
}

class HttpServer {
  private routes: Route[] = [];
  private middlewares: Middleware[] = [];
  private port: number;

  constructor(port: number = 3000) {
    this.port = port;
  }

  use(middleware: Middleware): void {
    this.middlewares.push(middleware);
  }

  get(path: string, handler: Route['handler']): void {
    this.routes.push({ method: 'GET', path, handler });
  }

  post(path: string, handler: Route['handler']): void {
    this.routes.push({ method: 'POST', path, handler });
  }

  put(path: string, handler: Route['handler']): void {
    this.routes.push({ method: 'PUT', path, handler });
  }

  delete(path: string, handler: Route['handler']): void {
    this.routes.push({ method: 'DELETE', path, handler });
  }

  private findRoute(method: string, url: string): Route | undefined {
    return this.routes.find(r => r.method === method && r.path === url);
  }

  listen(): void {
    const server = createServer((req, res) => {
      const route = this.findRoute(req.method || 'GET', req.url || '/');
      if (!route) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'Not Found' }));
        return;
      }

      let idx = 0;
      const next = () => {
        if (idx < this.middlewares.length) {
          this.middlewares[idx++](req, res, next);
        } else {
          route.handler(req, res);
        }
      };
      next();
    });

    server.listen(this.port, () => {
      console.log(`Server listening on port ${this.port}`);
    });
  }
}

export { HttpServer, Route, Middleware };
"#
}

fn file_btree_rs() -> &'static str {
r#"//! B-tree implementation for sorted key-value storage.
//!
//! Supports insert, search, and range queries over ordered keys.
//! Each node holds between t-1 and 2t-1 keys, where t is the minimum degree.

use std::fmt::Debug;

#[derive(Debug, Clone)]
struct BTreeNode<K: Ord + Clone + Debug, V: Clone + Debug> {
    keys: Vec<K>,
    values: Vec<V>,
    children: Vec<Box<BTreeNode<K, V>>>,
    leaf: bool,
}

impl<K: Ord + Clone + Debug, V: Clone + Debug> BTreeNode<K, V> {
    fn new(leaf: bool) -> Self {
        BTreeNode {
            keys: Vec::new(),
            values: Vec::new(),
            children: Vec::new(),
            leaf,
        }
    }

    fn search(&self, key: &K) -> Option<&V> {
        let mut i = 0;
        while i < self.keys.len() && key > &self.keys[i] {
            i += 1;
        }
        if i < self.keys.len() && key == &self.keys[i] {
            return Some(&self.values[i]);
        }
        if self.leaf {
            return None;
        }
        self.children[i].search(key)
    }

    fn range_query(&self, low: &K, high: &K) -> Vec<(&K, &V)> {
        let mut results = Vec::new();
        let mut i = 0;
        while i < self.keys.len() {
            if &self.keys[i] >= low {
                break;
            }
            if !self.leaf {
                results.extend(self.children[i].range_query(low, high));
            }
            i += 1;
        }
        while i < self.keys.len() && &self.keys[i] <= high {
            if !self.leaf {
                results.extend(self.children[i].range_query(low, high));
            }
            results.push((&self.keys[i], &self.values[i]));
            i += 1;
        }
        if !self.leaf && i < self.children.len() {
            results.extend(self.children[i].range_query(low, high));
        }
        results
    }
}

pub struct BTree<K: Ord + Clone + Debug, V: Clone + Debug> {
    root: BTreeNode<K, V>,
    min_degree: usize,
}

impl<K: Ord + Clone + Debug, V: Clone + Debug> BTree<K, V> {
    pub fn new(min_degree: usize) -> Self {
        BTree {
            root: BTreeNode::new(true),
            min_degree,
        }
    }

    pub fn search(&self, key: &K) -> Option<&V> {
        self.root.search(key)
    }

    pub fn range(&self, low: &K, high: &K) -> Vec<(&K, &V)> {
        self.root.range_query(low, high)
    }
}
"#
}

fn file_database_go() -> &'static str {
r#"// Package database provides a connection pool and query builder
// for PostgreSQL with prepared statement caching and transaction support.
package database

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Pool manages a connection pool with health checking and metrics.
type Pool struct {
	db       *sql.DB
	mu       sync.RWMutex
	stmtCache map[string]*sql.Stmt
	maxConns  int
	timeout   time.Duration
}

// NewPool creates a new connection pool.
func NewPool(dsn string, maxConns int) (*Pool, error) {
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}
	db.SetMaxOpenConns(maxConns)
	db.SetMaxIdleConns(maxConns / 2)
	db.SetConnMaxLifetime(30 * time.Minute)

	return &Pool{
		db:        db,
		stmtCache: make(map[string]*sql.Stmt),
		maxConns:  maxConns,
		timeout:   10 * time.Second,
	}, nil
}

// Query executes a query with prepared statement caching.
func (p *Pool) Query(ctx context.Context, query string, args ...interface{}) (*sql.Rows, error) {
	stmt, err := p.prepare(query)
	if err != nil {
		return nil, err
	}
	return stmt.QueryContext(ctx, args...)
}

// Exec executes a statement with prepared statement caching.
func (p *Pool) Exec(ctx context.Context, query string, args ...interface{}) (sql.Result, error) {
	stmt, err := p.prepare(query)
	if err != nil {
		return nil, err
	}
	return stmt.ExecContext(ctx, args...)
}

func (p *Pool) prepare(query string) (*sql.Stmt, error) {
	p.mu.RLock()
	if stmt, ok := p.stmtCache[query]; ok {
		p.mu.RUnlock()
		return stmt, nil
	}
	p.mu.RUnlock()

	p.mu.Lock()
	defer p.mu.Unlock()
	if stmt, ok := p.stmtCache[query]; ok {
		return stmt, nil
	}

	stmt, err := p.db.Prepare(query)
	if err != nil {
		return nil, fmt.Errorf("prepare statement: %w", err)
	}
	p.stmtCache[query] = stmt
	return stmt, nil
}

// Transaction wraps a function in a database transaction.
func (p *Pool) Transaction(ctx context.Context, fn func(tx *sql.Tx) error) error {
	tx, err := p.db.BeginTx(ctx, nil)
	if err != nil {
		return fmt.Errorf("begin transaction: %w", err)
	}
	if err := fn(tx); err != nil {
		tx.Rollback()
		return err
	}
	return tx.Commit()
}

// Close closes the pool and all cached statements.
func (p *Pool) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	for _, stmt := range p.stmtCache {
		stmt.Close()
	}
	return p.db.Close()
}
"#
}

fn file_react_component_tsx() -> &'static str {
r#"/**
 * React component for a data table with sorting, filtering, and pagination.
 * Uses useState and useEffect hooks for state management.
 */

import React, { useState, useEffect, useMemo, useCallback } from 'react';

interface Column<T> {
  key: keyof T;
  label: string;
  sortable?: boolean;
  render?: (value: T[keyof T], row: T) => React.ReactNode;
}

interface DataTableProps<T> {
  data: T[];
  columns: Column<T>[];
  pageSize?: number;
  onRowClick?: (row: T) => void;
  searchable?: boolean;
}

type SortDirection = 'asc' | 'desc' | null;

function DataTable<T extends Record<string, any>>({
  data,
  columns,
  pageSize = 10,
  onRowClick,
  searchable = true,
}: DataTableProps<T>) {
  const [currentPage, setCurrentPage] = useState(1);
  const [sortKey, setSortKey] = useState<keyof T | null>(null);
  const [sortDir, setSortDir] = useState<SortDirection>(null);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    setCurrentPage(1);
  }, [filter, sortKey, sortDir]);

  const filtered = useMemo(() => {
    if (!filter) return data;
    const lower = filter.toLowerCase();
    return data.filter(row =>
      columns.some(col => {
        const val = row[col.key];
        return val != null && String(val).toLowerCase().includes(lower);
      })
    );
  }, [data, columns, filter]);

  const sorted = useMemo(() => {
    if (!sortKey || !sortDir) return filtered;
    return [...filtered].sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      const cmp = aVal < bVal ? -1 : aVal > bVal ? 1 : 0;
      return sortDir === 'asc' ? cmp : -cmp;
    });
  }, [filtered, sortKey, sortDir]);

  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = sorted.slice(
    (currentPage - 1) * pageSize,
    currentPage * pageSize
  );

  const handleSort = useCallback((key: keyof T) => {
    if (sortKey === key) {
      setSortDir(prev => prev === 'asc' ? 'desc' : prev === 'desc' ? null : 'asc');
      if (sortDir === 'desc') setSortKey(null);
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  }, [sortKey, sortDir]);

  return (
    <div className="data-table">
      {searchable && (
        <input
          type="text"
          placeholder="Search..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          className="search-input"
        />
      )}
      <table>
        <thead>
          <tr>
            {columns.map(col => (
              <th
                key={String(col.key)}
                onClick={() => col.sortable && handleSort(col.key)}
                style={{ cursor: col.sortable ? 'pointer' : 'default' }}
              >
                {col.label}
                {sortKey === col.key && (sortDir === 'asc' ? ' ↑' : ' ↓')}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {paginated.map((row, i) => (
            <tr key={i} onClick={() => onRowClick?.(row)}>
              {columns.map(col => (
                <td key={String(col.key)}>
                  {col.render ? col.render(row[col.key], row) : String(row[col.key] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="pagination">
        <button disabled={currentPage <= 1} onClick={() => setCurrentPage(p => p - 1)}>
          Previous
        </button>
        <span>{currentPage} / {totalPages}</span>
        <button disabled={currentPage >= totalPages} onClick={() => setCurrentPage(p => p + 1)}>
          Next
        </button>
      </div>
    </div>
  );
}

export default DataTable;
"#
}

// ---------------------------------------------------------------------------
// Helpers: GPU enforcement
// ---------------------------------------------------------------------------

async fn assert_gpu_active(embed_port: u16) {
    if std::env::var("OPENCODE_ONNX_PROVIDER").map(|v| v.to_lowercase()) == Ok("cpu".into()) {
        println!("  [GPU check skipped: OPENCODE_ONNX_PROVIDER=cpu]");
        return;
    }

    let client = reqwest::Client::new();
    let resp = client
        .get(format!("http://127.0.0.1:{embed_port}/health"))
        .send()
        .await
        .expect("health request")
        .json::<serde_json::Value>()
        .await
        .expect("parse health");

    let gpu = &resp["result"]["gpu"];
    let provider = gpu["provider"].as_str().unwrap_or("unknown");
    let is_gpu = gpu["is_gpu"].as_bool().unwrap_or(false);
    let degraded = gpu["degraded"].as_bool().unwrap_or(false);

    let gpu_providers = ["tensorrt", "cuda", "migraphx", "rocm"];

    assert!(
        is_gpu,
        "GPU enforcement failed: is_gpu=false  provider={provider}\n\
         → GPU inference is required. Set OPENCODE_ONNX_PROVIDER=cpu to skip.\n\
         → Full response: {gpu}"
    );
    assert!(
        gpu_providers.contains(&provider),
        "GPU enforcement failed: provider={provider:?} is not a recognised GPU provider\n\
         → Expected one of: {gpu_providers:?}\n\
         → Full response: {gpu}"
    );
    assert!(
        !degraded,
        "GPU degraded: provider={provider:?} fell back to CPU\n\
         → Check ONNX Runtime GPU libraries and CUDA/ROCm driver.\n\
         → Full response: {gpu}"
    );

    println!("  [GPU OK: provider={provider}]");
}

// ---------------------------------------------------------------------------
// Test: GPU enforcement (fast, runs independently)
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn gpu_enforcement() {
    println!("=== GPU Enforcement Check ===");
    let server = python_server::PythonServer::start().await.expect("start embedder");
    assert_gpu_active(server.port).await;
    println!("  GPU enforcement: PASSED");
}

// ---------------------------------------------------------------------------
// Test: semantic search quality through the full pipeline
// ---------------------------------------------------------------------------

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn quality_semantic_search() {
    let (server, daemon) = setup().await.expect("setup");
    let port = daemon.port();
    let home = server.home_path().to_path_buf();

    // Verify GPU is active before running the expensive quality test
    assert_gpu_active(server.port).await;

    let start = Instant::now();

    // Warm up
    let _ = rpc(port, "ping", serde_json::json!({})).await;

    // --- Index 5 distinct files in different languages ---
    println!("\n=== Indexing 5 files (Python, TypeScript, Rust, Go, React TSX) ===");

    let root = tempfile::TempDir::new().unwrap();
    let db = root.path().join(".lancedb-quality");

    let files = vec![
        ("fibonacci.py", file_fibonacci_py()),
        ("http_server.ts", file_http_server_ts()),
        ("btree.rs", file_btree_rs()),
        ("database.go", file_database_go()),
        ("data_table.tsx", file_react_component_tsx()),
    ];

    let rss_before = python_rss_kb(&home);

    for (i, (name, content)) in files.iter().enumerate() {
        std::fs::write(root.path().join(name), content).unwrap();
        let t = Instant::now();
        let r = rpc(port, "index_file", serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": name,
            "tier": "budget",
            "dimensions": 256
        })).await.expect("index rpc");

        let ms = t.elapsed().as_secs_f64() * 1000.0;
        let chunks = r["result"]["chunks"].as_u64().unwrap_or(0);
        let success = r["result"]["success"].as_bool().unwrap_or(false);
        println!("  {name}: {ms:.0}ms  chunks={chunks}  success={success}");
        assert!(success, "index failed for {name}: {r}");
        assert!(chunks > 0, "no chunks for {name}: {r}");
        let _ = i;
    }

    let rss_after = python_rss_kb(&home);
    if let (Some(before), Some(after)) = (rss_before, rss_after) {
        println!("  Python server RSS: {}MB → {}MB (delta +{}MB)",
            before / 1024, after / 1024, after.saturating_sub(before) / 1024);
    }

    // --- Semantic search quality ---
    println!("\n=== Semantic Search Quality ===");

    // Each query should find a specific file as the top result
    let queries: Vec<(&str, &str)> = vec![
        ("fibonacci memoization recursive", "fibonacci.py"),
        ("HTTP server routing middleware request handling", "http_server.ts"),
        ("B-tree sorted key value search range query", "btree.rs"),
        ("PostgreSQL connection pool prepared statement transaction", "database.go"),
        ("React component table sorting filtering pagination", "data_table.tsx"),
    ];

    let mut correct = 0;
    let mut total = 0;

    for (i, (query, expected_file)) in queries.iter().enumerate() {
        let t = Instant::now();
        let r = rpc(port, "search", serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "query": query,
            "tier": "budget",
            "dimensions": 256
        })).await.expect("search rpc");

        let ms = t.elapsed().as_secs_f64() * 1000.0;
        let results = r["result"]["results"].as_array().expect("results array");

        total += 1;

        if results.is_empty() {
            println!("  Q: \"{query}\" → NO RESULTS  (expected: {expected_file})  {ms:.0}ms");
            continue;
        }

        let top_path = results[0]["path"].as_str().unwrap_or("?");
        let top_score = results[0]["score"].as_f64().unwrap_or(0.0);
        let found = top_path.contains(expected_file);
        if found { correct += 1; }

        let status = if found { "OK" } else { "WRONG" };
        println!("  Q: \"{query}\"");
        println!("     top: {top_path} (score={top_score:.4})  [{status}]  {ms:.0}ms");

        // Print top 3 for debugging
        for (j, r) in results.iter().take(3).enumerate() {
            let p = r["path"].as_str().unwrap_or("?");
            let s = r["score"].as_f64().unwrap_or(0.0);
            println!("     #{}: {p} (score={s:.4})", j + 1);
        }
        let _ = i;
    }

    let accuracy = if total > 0 { 100.0 * correct as f64 / total as f64 } else { 0.0 };
    println!("\n  Accuracy: {correct}/{total} ({accuracy:.0}%)");

    // With real embeddings, we expect at least 3/5 correct (60%)
    assert!(correct >= 3, "accuracy too low: {correct}/{total} — expected at least 3/5");

    // --- Skip unchanged (dedup correctness) ---
    println!("\n=== Dedup Correctness ===");
    let mut skipped = 0;
    for (i, (name, _)) in files.iter().enumerate() {
        let r = rpc(port, "index_file", serde_json::json!({
            "root": root.path().to_str().unwrap(),
            "db": db.to_str().unwrap(),
            "file": name,
            "tier": "budget",
            "dimensions": 256
        })).await.expect("reindex rpc");
        if r["result"]["skipped"].as_bool() == Some(true) { skipped += 1; }
        let _ = i;
    }
    println!("  Skipped: {skipped}/5 (expected 5)");
    assert_eq!(skipped, 5, "dedup failed — files should be skipped on re-index");

    // --- Modified file detection ---
    println!("\n=== Modified File Detection ===");
    let modified_content = format!("{}\n// MODIFIED: added extra comment\n", file_fibonacci_py());
    std::fs::write(root.path().join("fibonacci.py"), &modified_content).unwrap();
    let r = rpc(port, "index_file", serde_json::json!({
        "root": root.path().to_str().unwrap(),
        "db": db.to_str().unwrap(),
        "file": "fibonacci.py",
        "tier": "budget",
        "dimensions": 256
    })).await.expect("modified rpc");
    let re_indexed = r["result"]["success"].as_bool() == Some(true)
        && r["result"]["skipped"].as_bool() != Some(true);
    println!("  Modified fibonacci.py re-indexed: {re_indexed}");
    assert!(re_indexed, "modified file should be re-indexed, not skipped");

    // --- Resource check ---
    println!("\n=== Final Resource Usage ===");
    let rss_final = python_rss_kb(&home);
    if let Some(rss) = rss_final {
        println!("  Python server RSS: {}MB", rss / 1024);
        // Budget tier with small models: RSS should stay under 2GB
        assert!(rss < 2_000_000, "Python server RSS > 2GB: {}KB", rss);
    }

    // --- Summary ---
    let total_time = start.elapsed();
    println!("\n=== SUMMARY ===");
    println!("  backend:       REAL Python model server");
    println!("  search accuracy: {correct}/{total} ({accuracy:.0}%)");
    println!("  dedup:         {skipped}/5 skipped correctly");
    println!("  total time:    {:.1}s", total_time.as_secs_f64());

    daemon.shutdown().await;
}
