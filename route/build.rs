fn main() {
    // `cargo test`'s binary links `libpython` (an extension module doesn't),
    // and `uv`'s Python keeps it outside the loader's search path,
    // so record its directory in the binary.
    pyo3_build_config::add_libpython_rpath_link_args();

    // OR-Tools' libraries, for CP-SAT, from `$ORTOOLS_PREFIX` (see `scripts/fetch-ortools.sh`).
    // The `cp_sat` crate records them only in its own link arguments,
    // which don't reach the extension module or test binaries built here.
    println!("cargo:rerun-if-env-changed=ORTOOLS_PREFIX");
    let prefix = std::env::var("ORTOOLS_PREFIX").expect("`$ORTOOLS_PREFIX`, set by `mise.toml`");
    for lib in ["lib", "lib64"] {
        let dir = std::path::Path::new(&prefix).join(lib);
        if dir.exists() {
            println!("cargo:rustc-link-arg=-Wl,-rpath,{}", dir.display());
            println!("cargo:rustc-link-search=native={}", dir.display());
        }
    }
}
