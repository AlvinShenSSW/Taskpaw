//! #299 §3: the planned restart of the backend the shell owns.
//!
//! A watcher thread polls the owned child every 250 ms. Exit code 75 (the
//! backend's `restart.EXIT_CODE`, sent only after the UI asked for 立即重启)
//! respawns the same command: the old Job is dropped first (Windows: any
//! straggler of the old tree dies), the new child gets its own Job, its
//! readiness line is read (30 s), its control descriptor is loaded and the
//! `main` window is rebuilt with the new init script, at the old window's size,
//! position and maximized state (review IR9). A failure on that path ends in
//! the existing fatal startup dialog. Any other exit keeps today's behaviour:
//! the watcher just stops. On Unix the watcher looks at the child without
//! reaping it (review IR6), so the old process group is killed while its
//! exited leader still pins the group id.
//!
//! The decisions are pure and generic so they are unit-tested with a fake
//! child and a fake spawner; `main.rs` supplies the real ones.

use std::time::Duration;

pub const RESTART_EXIT_CODE: i32 = 75;
pub const POLL_INTERVAL: Duration = Duration::from_millis(250);
pub const MIN_WIDTH: f64 = 720.0;
pub const MIN_HEIGHT: f64 = 480.0;

/// The old `main` window's place, in logical pixels (review IR9).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Geometry {
    pub width: f64,
    pub height: f64,
    pub position: Option<(f64, f64)>,
    pub maximized: bool,
}

/// Physical inner size / outer position at `scale` → the logical geometry the
/// window builder takes (never below the minimum size).
pub fn logical_geometry(
    size: (u32, u32),
    position: Option<(i32, i32)>,
    scale: f64,
    maximized: bool,
) -> Geometry {
    let scale = if scale.is_finite() && scale > 0.0 { scale } else { 1.0 };
    Geometry {
        width: (f64::from(size.0) / scale).max(MIN_WIDTH),
        height: (f64::from(size.1) / scale).max(MIN_HEIGHT),
        position: position.map(|(x, y)| (f64::from(x) / scale, f64::from(y) / scale)),
        maximized,
    }
}

/// One look at the owned child.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Poll {
    /// Still running.
    Running,
    /// Exited (the code, None when killed by a signal).
    Exited(Option<i32>),
    /// No child is owned any more (the app is quitting, or attach mode).
    Gone,
}

/// What the watcher does after a poll.
#[derive(Debug, PartialEq, Eq)]
pub enum Next {
    Wait,
    Restart,
    Stop,
}

pub fn classify(poll: Poll) -> Next {
    match poll {
        Poll::Running => Next::Wait,
        Poll::Exited(Some(RESTART_EXIT_CODE)) => Next::Restart,
        Poll::Exited(_) | Poll::Gone => Next::Stop,
    }
}

/// The steps of one restart, in order. `main.rs` implements them on the real
/// backend state; tests record them.
pub trait Respawn {
    type Child;
    type Ready;
    /// Drop the old process tree's Job (Windows) — its stragglers die.
    fn release_old(&mut self);
    /// Start the same backend command again.
    fn spawn(&mut self) -> Result<Self::Child, String>;
    /// Put the new child in a fresh kill-on-close Job (always true off Windows).
    /// On failure it must kill `child` itself (no orphan).
    fn adopt(&mut self, child: &mut Self::Child) -> Result<(), String>;
    /// Hand the child to the shell's owned state (so quitting kills it).
    fn install(&mut self, child: Self::Child);
    /// Read the readiness line and load the new control descriptor.
    fn ready(&mut self) -> Result<Self::Ready, String>;
    /// Replace the `main` window with one built for `ready`.
    fn rebuild(&mut self, ready: Self::Ready) -> Result<(), String>;
}

pub fn restart<R: Respawn>(r: &mut R) -> Result<(), String> {
    r.release_old();
    let mut child = r.spawn()?;
    r.adopt(&mut child)?;
    r.install(child);
    let ready = r.ready()?;
    r.rebuild(ready)
}

/// Watch until the child is gone or exits for good. Each exit 75 restarts once;
/// a failed restart calls `fatal` (which does not return in production).
pub fn supervise<R: Respawn>(
    mut poll: impl FnMut() -> Poll,
    respawn: &mut R,
    mut sleep: impl FnMut(),
    mut fatal: impl FnMut(String),
) {
    loop {
        match classify(poll()) {
            Next::Wait => sleep(),
            Next::Stop => return,
            Next::Restart => {
                if let Err(message) = restart(respawn) {
                    fatal(message);
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct Fake {
        log: Vec<String>,
        spawned: u32,
        fail_ready: bool,
        fail_adopt: bool,
    }

    impl Respawn for Fake {
        type Child = u32;
        type Ready = String;
        fn release_old(&mut self) {
            self.log.push("release_old".into());
        }
        fn spawn(&mut self) -> Result<u32, String> {
            self.spawned += 1;
            self.log.push(format!("spawn {}", self.spawned));
            Ok(self.spawned)
        }
        fn adopt(&mut self, child: &mut u32) -> Result<(), String> {
            self.log.push(format!("adopt {child}"));
            if self.fail_adopt {
                Err("job".into())
            } else {
                Ok(())
            }
        }
        fn install(&mut self, child: u32) {
            self.log.push(format!("install {child}"));
        }
        fn ready(&mut self) -> Result<String, String> {
            self.log.push("ready".into());
            if self.fail_ready {
                Err("后端在 30 秒内未报告启动状态。".into())
            } else {
                Ok("descriptor".into())
            }
        }
        fn rebuild(&mut self, ready: String) -> Result<(), String> {
            self.log.push(format!("rebuild {ready}"));
            Ok(())
        }
    }

    fn run(polls: &[Poll], fake: &mut Fake) -> (Vec<String>, u32) {
        let mut queue = polls.iter().copied();
        let mut fatal = Vec::new();
        let mut sleeps = 0;
        supervise(
            || queue.next().unwrap_or(Poll::Gone),
            fake,
            || sleeps += 1,
            |m| fatal.push(m),
        );
        (fatal, sleeps)
    }

    #[test]
    fn exit_75_respawns_once_in_order_and_rebuilds() {
        let mut fake = Fake::default();
        let (fatal, sleeps) = run(
            &[Poll::Running, Poll::Exited(Some(75)), Poll::Running, Poll::Exited(Some(0))],
            &mut fake,
        );
        assert!(fatal.is_empty());
        assert_eq!(sleeps, 2);
        assert_eq!(
            fake.log,
            ["release_old", "spawn 1", "adopt 1", "install 1", "ready", "rebuild descriptor"]
        );
    }

    #[test]
    fn every_other_exit_does_nothing() {
        for poll in [Poll::Exited(Some(0)), Poll::Exited(Some(1)), Poll::Exited(None), Poll::Gone] {
            let mut fake = Fake::default();
            let (fatal, _) = run(&[poll, Poll::Exited(Some(75))], &mut fake);
            assert!(fatal.is_empty() && fake.log.is_empty(), "{poll:?}");
        }
    }

    #[test]
    fn a_failed_respawn_readiness_is_fatal() {
        let mut fake = Fake { fail_ready: true, ..Fake::default() };
        let (fatal, _) = run(&[Poll::Exited(Some(75)), Poll::Running], &mut fake);
        assert_eq!(fatal.len(), 1);
        assert!(!fake.log.iter().any(|s| s.starts_with("rebuild")));
    }

    #[test]
    fn the_new_child_is_adopted_by_a_new_job_before_it_is_installed() {
        let mut fake = Fake { fail_adopt: true, ..Fake::default() };
        let (fatal, _) = run(&[Poll::Exited(Some(75))], &mut fake);
        assert_eq!(fatal.len(), 1);
        assert_eq!(fake.log, ["release_old", "spawn 1", "adopt 1"]);
    }

    #[test]
    fn the_rebuilt_window_keeps_size_position_and_maximized() {
        // review IR9: physical pixels at 200 % become logical ones
        let g = logical_geometry((2200, 1440), Some((300, 150)), 2.0, true);
        assert_eq!(
            g,
            Geometry { width: 1100.0, height: 720.0, position: Some((150.0, 75.0)), maximized: true }
        );
        // never below the minimum size; a bad scale factor counts as 1
        let g = logical_geometry((100, 100), None, 0.0, false);
        assert_eq!(g, Geometry { width: 720.0, height: 480.0, position: None, maximized: false });
        let g = logical_geometry((1500, 900), Some((-1200, 40)), f64::NAN, false);
        assert_eq!(g.position, Some((-1200.0, 40.0)));
        assert_eq!((g.width, g.height), (1500.0, 900.0));
    }

    #[test]
    fn two_planned_restarts_restart_twice() {
        let mut fake = Fake::default();
        run(&[Poll::Exited(Some(75)), Poll::Running, Poll::Exited(Some(75))], &mut fake);
        assert_eq!(fake.spawned, 2);
    }
}
