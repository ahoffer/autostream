//! A shared shutdown flag that threads can wait on with a timeout.

use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

/// Cloneable handle to one flag; once set it stays set.
#[derive(Clone, Default)]
pub struct StopSignal {
    inner: Arc<(Mutex<bool>, Condvar)>,
}

impl StopSignal {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self) {
        let (flag, wakeup) = &*self.inner;
        *flag.lock().unwrap_or_else(|poisoned| poisoned.into_inner()) = true;
        wakeup.notify_all();
    }

    pub fn is_set(&self) -> bool {
        let (flag, _) = &*self.inner;
        *flag.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Block until the signal is set or the timeout passes; returns whether it is set.
    pub fn wait(&self, timeout: Duration) -> bool {
        let (flag, wakeup) = &*self.inner;
        let deadline = Instant::now() + timeout;
        let mut stopped = flag.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        while !*stopped {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            let (guard, _) = wakeup
                .wait_timeout(stopped, deadline - now)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            stopped = guard;
        }
        *stopped
    }

    /// Block until the signal is set.
    pub fn wait_forever(&self) {
        let (flag, wakeup) = &*self.inner;
        let mut stopped = flag.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        while !*stopped {
            stopped = wakeup
                .wait(stopped)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn wait_returns_early_once_set() {
        let signal = StopSignal::new();
        let setter = signal.clone();
        thread::spawn(move || {
            thread::sleep(Duration::from_millis(20));
            setter.set();
        });
        let started = Instant::now();
        assert!(signal.wait(Duration::from_secs(5)));
        assert!(started.elapsed() < Duration::from_secs(4));
        assert!(signal.is_set());
    }

    #[test]
    fn wait_times_out_while_unset() {
        let signal = StopSignal::new();
        assert!(!signal.wait(Duration::from_millis(10)));
    }
}
