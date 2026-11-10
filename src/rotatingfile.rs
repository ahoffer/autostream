//! An append-only text file that rolls over by size, keeping a fixed number of backups.
//!
//! Mirrors the rotation the Python version used: when a line would push the
//! file to the size limit, `name` becomes `name.1`, `name.1` becomes `name.2`,
//! and so on up to the backup count, and writing continues in a new `name`.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

pub struct RotatingFile {
    path: PathBuf,
    max_bytes: u64,
    backup_count: u32,
    file: File,
    size: u64,
}

impl RotatingFile {
    pub fn open(path: &Path, max_bytes: u64, backup_count: u32) -> io::Result<Self> {
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let size = file.metadata()?.len();
        Ok(Self {
            path: path.to_path_buf(),
            max_bytes,
            backup_count,
            file,
            size,
        })
    }

    /// Append one line, rolling the file over first when the line would not fit.
    pub fn write_line(&mut self, text: &str) -> io::Result<()> {
        let line_length = text.len() as u64 + 1;
        if self.max_bytes > 0 && self.size + line_length >= self.max_bytes {
            self.roll_over()?;
        }
        self.file.write_all(text.as_bytes())?;
        self.file.write_all(b"\n")?;
        self.file.flush()?;
        self.size += line_length;
        Ok(())
    }

    fn roll_over(&mut self) -> io::Result<()> {
        if self.backup_count > 0 {
            let oldest = self.backup_path(self.backup_count);
            if oldest.exists() {
                fs::remove_file(&oldest)?;
            }
            for index in (1..self.backup_count).rev() {
                let source = self.backup_path(index);
                if source.exists() {
                    fs::rename(&source, self.backup_path(index + 1))?;
                }
            }
            fs::rename(&self.path, self.backup_path(1))?;
        } else {
            fs::remove_file(&self.path)?;
        }
        self.file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        self.size = 0;
        Ok(())
    }

    fn backup_path(&self, index: u32) -> PathBuf {
        let mut name = self.path.as_os_str().to_os_string();
        name.push(format!(".{index}"));
        PathBuf::from(name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rolls_over_by_size_and_keeps_the_newest_backups() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("stream.log");
        let mut file = RotatingFile::open(&path, 20, 2).unwrap();
        for line in ["first line", "second line", "third line", "fourth line"] {
            file.write_line(line).unwrap();
        }
        assert_eq!(fs::read_to_string(&path).unwrap(), "fourth line\n");
        assert_eq!(
            fs::read_to_string(directory.path().join("stream.log.1")).unwrap(),
            "third line\n"
        );
        assert_eq!(
            fs::read_to_string(directory.path().join("stream.log.2")).unwrap(),
            "second line\n"
        );
        assert!(!directory.path().join("stream.log.3").exists());
    }

    #[test]
    fn appends_to_an_existing_file_and_counts_its_size() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("stream.log");
        fs::write(&path, "existing\n").unwrap();
        let mut file = RotatingFile::open(&path, 0, 1).unwrap();
        file.write_line("more").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "existing\nmore\n");
    }
}
