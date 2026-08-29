import type { JobResponse } from "../api/types";
import { elapsedSince, queueLabel, statusLabel } from "./labels";

export function activitySentence(job: JobResponse): string {
  const activity = queueLabel(job.queue);
  if (job.status === "RUNNING") {
    if (job.queue === "INTEGRATION") {
      return job.work_item_human_id
        ? `Combining the approved implementation for ${job.work_item_human_id} into the iteration candidate.`
        : "Combining the approved implementation into the iteration candidate.";
    }
    if (job.work_item_human_id) {
      return `${activity} is evaluating ${job.work_item_human_id}.`;
    }
    return `${activity} is in progress.`;
  }
  if (job.status === "BLOCKED") {
    return `${activity} is blocked${job.last_error ? `: ${job.last_error}` : "."}`;
  }
  return `${activity} is ${statusLabel(job.status).toLowerCase()}.`;
}

export function nextStepSentence(job: JobResponse): string {
  const queue = job.queue;
  if (queue === "DELIVERY") {
    return "Independent reviews begin after delivery succeeds.";
  }
  if (queue === "INTEGRATION") {
    return "Release verification begins after integration succeeds.";
  }
  if (queue.startsWith("ASSURANCE")) {
    return "Release verification begins after this review succeeds.";
  }
  if (queue === "RELEASE") {
    return "The iteration is complete if release succeeds.";
  }
  if (queue === "PM") {
    return "Delivery work is created after planning is accepted.";
  }
  return "The next governed job starts when this one succeeds.";
}

export function describeActiveWork(job: JobResponse, now = Date.now()) {
  return {
    title: queueLabel(job.queue),
    status: statusLabel(job.status),
    sentence: activitySentence(job),
    objective: job.work_item_human_id ?? "Objective not reported",
    started: elapsedSince(job.started_at, now),
    next: nextStepSentence(job),
  };
}

export function runningJobs(jobs: JobResponse[]): JobResponse[] {
  return jobs.filter((job) => job.status === "RUNNING" || job.status === "LEASED");
}

export function blockedJobs(jobs: JobResponse[]): JobResponse[] {
  return jobs.filter((job) => job.status === "BLOCKED" || job.status === "FAILED");
}

export function finishedJobs(jobs: JobResponse[]): JobResponse[] {
  return jobs.filter((job) => job.status === "SUCCEEDED");
}
