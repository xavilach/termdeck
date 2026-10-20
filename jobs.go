package main

import (
	"context"
	"fmt"
	"path/filepath"
	"sort"
	"time"
)

// Jobs: what a session does over time (a script, an automatic send, a file being sent, a transfer) runs on
// the server, with its progress and a Stop. The page shows them in one place; they go on while the page
// reloads, and end when the port closes.

type job struct {
	ID     int    `json:"id"`
	Kind   string `json:"kind"`  // script, auto, file or transfer
	Name   string `json:"name"`  // the script, the data, the file
	Done   int64  `json:"done"`  // progress: bytes, or rounds
	Total  int64  `json:"total"` // 0 when there is no end
	cancel context.CancelFunc
}

// startJob runs fn in the background as a job of the open port. A job of the same kind and name is stopped
// first (a script started again, a new automatic send). fn returns what went wrong, or nil.
func (t *termSession) startJob(kind, name string, total int64, fn func(ctx context.Context, j *job) error) (*job, error) {
	t.mu.Lock()
	runCtx := t.runCtx
	if runCtx == nil || (t.port == nil && t.tun == nil) {
		t.mu.Unlock()
		return nil, fmt.Errorf("the port is closed")
	}
	for _, other := range t.jobs {
		if other.Kind == kind && (other.Name == name || kind == "auto" || kind == "transfer") {
			other.cancel()
		}
	}
	ctx, cancel := context.WithCancel(runCtx)
	t.jobSeq++
	j := &job{ID: t.jobSeq, Kind: kind, Name: name, Total: total, cancel: cancel}
	if t.jobs == nil {
		t.jobs = map[int]*job{}
	}
	t.jobs[j.ID] = j
	t.mu.Unlock()
	t.sendJobs()
	go func() {
		err := fn(ctx, j)
		stopped := ctx.Err() != nil // stopped, or the port closed
		cancel()
		t.mu.Lock()
		delete(t.jobs, j.ID)
		t.mu.Unlock()
		t.sendJobs()
		t.jobEnded(j, err, stopped)
	}()
	return j, nil
}

// jobEnded notes in the stream how a job ended.
func (t *termSession) jobEnded(j *job, err error, stopped bool) {
	what := map[string]string{"script": "script " + j.Name, "auto": "automatic send", "file": "file " + j.Name, "transfer": j.Name}[j.Kind]
	switch {
	case err != nil && !stopped:
		t.note(what + ": " + err.Error())
	case j.Kind == "file" && j.Done < j.Total:
		t.note(fmt.Sprintf("%s stopped after %d bytes", what, j.Done))
	case j.Kind == "file":
		t.note(fmt.Sprintf("%s sent (%d bytes)", what, j.Done))
	case j.Kind == "script" && stopped && !t.closing():
		t.note(what + " stopped")
	case j.Kind == "script" && !stopped:
		t.note(what + " done")
	}
}

// closing says whether the port is closed (the jobs then end without a word of their own).
func (t *termSession) closing() bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.port == nil && t.tun == nil
}

// progress sets how far a job is; the page is told at most a few times a second.
func (t *termSession) progress(j *job, done int64) {
	t.mu.Lock()
	j.Done = done
	due := time.Since(t.jobsSent) > 200*time.Millisecond
	t.mu.Unlock()
	if due {
		t.sendJobs()
	}
}

// listJobs is the jobs that run, oldest first.
func (t *termSession) listJobs() []job {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := make([]job, 0, len(t.jobs))
	for _, j := range t.jobs {
		out = append(out, *j)
	}
	sort.Slice(out, func(a, b int) bool { return out[a].ID < out[b].ID })
	return out
}

func (t *termSession) sendJobs() {
	list := t.listJobs()
	t.mu.Lock()
	t.jobsSent = time.Now()
	t.mu.Unlock()
	t.sendJSON(map[string]any{"type": "jobs", "jobs": list})
}

// stopJob stops one job; id 0 stops them all (and what waits in a slow send).
func (t *termSession) stopJob(id int) {
	t.mu.Lock()
	for _, j := range t.jobs {
		if id == 0 || j.ID == id {
			j.cancel()
		}
	}
	t.mu.Unlock()
	if id == 0 {
		t.flushOut()
	}
}

// ---------------------------------------------------------------- the jobs

// minAutoMs is the shortest pause between two automatic sends.
const minAutoMs = 10

// startAuto sends the send bar's data again and again, every ms milliseconds, until stopped.
func (t *termSession) startAuto(m clientMessage) {
	steps, err := payload(m.Format, m.Data, m.Check, endStep(m.End, m.EndCustom))
	if err != nil {
		t.notice(err.Error())
		return
	}
	every := time.Duration(max(m.Ms, minAutoMs)) * time.Millisecond
	_, err = t.startJob("auto", fmt.Sprintf("%s every %v", m.Data, every), 0, func(ctx context.Context, j *job) error {
		for round := int64(1); ; round++ {
			if err := t.runSteps(ctx, steps, stepRun{}); err != nil {
				return err
			}
			t.progress(j, round)
			if err := sleepCtx(ctx, every); err != nil {
				return nil
			}
		}
	})
	if err != nil {
		t.notice("automatic send: " + err.Error())
	}
}

// fileChunk is how much of a file goes out at once: small enough to stop at once, and to show progress.
const fileChunk = 256

// startFile sends the content of a file the page read, as it is.
func (t *termSession) startFile(name string, data []byte) {
	name = filepath.Base(name)
	_, err := t.startJob("file", name, int64(len(data)), func(ctx context.Context, j *job) error {
		return t.sendChunks(ctx, data, func(done int) { t.progress(j, int64(done)) })
	})
	if err != nil {
		t.notice("file " + name + ": " + err.Error())
	}
}

// sendChunks sends data a piece at a time, telling how far it is, until done or stopped.
func (t *termSession) sendChunks(ctx context.Context, data []byte, progress func(done int)) error {
	for at := 0; at < len(data); at += fileChunk {
		if ctx.Err() != nil {
			return nil
		}
		end := min(at+fileChunk, len(data))
		t.sendDataWait(data[at:end], nil, true) // the progress says what is out, also with a slow send
		progress(end)
	}
	return nil
}
