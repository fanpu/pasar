import { render, screen, fireEvent, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi, beforeEach } from "vitest";
import JobForm from "../components/JobForm.svelte";
import * as api from "../lib/api";
import { ApiError } from "../lib/api";
import { job, jobDetail } from "./fixtures";
import type { JobView } from "../lib/types";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return { ...actual, submitJob: vi.fn(), restartJob: vi.fn() };
});

const noop = (): void => {};
const noopDone = (_job: JobView): void => {};

function submittedJob(overrides: Partial<JobView> = {}): JobView {
  return job({ id: 99, ...overrides });
}

async function fillRequired(): Promise<void> {
  await fireEvent.input(screen.getByLabelText("command"), { target: { value: "python train.py" } });
  await fireEvent.input(screen.getByLabelText("directory"), { target: { value: "/home/you/run" } });
  await fireEvent.input(screen.getByLabelText("time"), { target: { value: "2h" } });
}

describe("JobForm (submit)", () => {
  beforeEach(() => {
    vi.mocked(api.submitJob).mockReset();
    vi.mocked(api.restartJob).mockReset();
    localStorage.clear();
  });

  it("uses the wider Modal size (it has several fields, unlike the default 360px dialogs)", () => {
    const { container } = render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });
    expect(container.querySelector(".modal.wide")).not.toBeNull();
  });

  it("sends mem: null, submitter web and the default bid for a whole-GPU submit", async () => {
    vi.mocked(api.submitJob).mockResolvedValue(submittedJob());
    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });

    await fillRequired();
    await fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(api.submitJob).toHaveBeenCalledWith(
        expect.objectContaining({
          command: "python train.py",
          cwd: "/home/you/run",
          time: "2h",
          mem: null,
          bid: 1000,
          submitter: "web",
        }),
      ),
    );
  });

  it("sends the shared memory size when shared is selected", async () => {
    vi.mocked(api.submitJob).mockResolvedValue(submittedJob());
    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });

    await fillRequired();
    await fireEvent.click(screen.getByLabelText("shared"));
    await fireEvent.input(screen.getByLabelText("memory size"), { target: { value: "24G" } });
    await fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    await waitFor(() =>
      expect(api.submitJob).toHaveBeenCalledWith(expect.objectContaining({ mem: "24G" })),
    );
  });

  it("requires a memory size for shared jobs without calling the API", async () => {
    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });

    await fillRequired();
    await fireEvent.click(screen.getByLabelText("shared"));
    await fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(await screen.findByText("memory size is required for shared jobs")).toBeInTheDocument();
    expect(api.submitJob).not.toHaveBeenCalled();
  });

  it("shows the API's validation message and leaves the form open without calling ondone", async () => {
    vi.mocked(api.submitJob).mockRejectedValue(new ApiError(422, "unknown duration: soon"));
    const ondone = vi.fn();
    render(JobForm, { mode: "submit", onclose: noop, ondone });

    await fillRequired();
    await fireEvent.click(screen.getByRole("button", { name: "Submit" }));

    expect(await screen.findByText("unknown duration: soon")).toBeInTheDocument();
    expect(ondone).not.toHaveBeenCalled();
  });

  it("Ctrl+Enter with an empty command does not call submitJob and shows the message", async () => {
    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });

    await fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });

    expect(
      await screen.findByText("command, directory and time are required"),
    ).toBeInTheDocument();
    expect(api.submitJob).not.toHaveBeenCalled();
  });

  it("Ctrl+Enter twice while the first submitJob is pending calls it once", async () => {
    let resolveSubmit!: (job: JobView) => void;
    vi.mocked(api.submitJob).mockImplementation(
      () => new Promise((resolve) => { resolveSubmit = resolve; }),
    );
    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });
    await fillRequired();

    await fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });
    await fireEvent.keyDown(window, { key: "Enter", ctrlKey: true });

    expect(api.submitJob).toHaveBeenCalledTimes(1);
    resolveSubmit(submittedJob());
    await waitFor(() => expect(api.submitJob).toHaveBeenCalledTimes(1));
  });

  it("saves the directory to localStorage on success and prefills it next render", async () => {
    vi.mocked(api.submitJob).mockResolvedValue(submittedJob());
    const { unmount } = render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });

    await fillRequired();
    await fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(api.submitJob).toHaveBeenCalled());
    expect(localStorage.getItem("pasar.cwd")).toBe("/home/you/run");
    unmount();

    render(JobForm, { mode: "submit", onclose: noop, ondone: noopDone });
    expect((screen.getByLabelText("directory") as HTMLInputElement).value).toBe("/home/you/run");
  });

  it("still calls ondone/onclose (no error shown) when saving the directory to localStorage throws", async () => {
    const onclose = vi.fn();
    const ondone = vi.fn();
    const submitted = submittedJob();
    vi.mocked(api.submitJob).mockResolvedValue(submitted);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    try {
      render(JobForm, { mode: "submit", onclose, ondone });

      await fillRequired();
      await fireEvent.click(screen.getByRole("button", { name: "Submit" }));

      await waitFor(() => expect(ondone).toHaveBeenCalledWith(submitted));
      expect(onclose).toHaveBeenCalled();
      expect(screen.queryByText(/quota exceeded/)).toBeNull();
    } finally {
      setItem.mockRestore();
    }
  });
});

describe("JobForm (restart)", () => {
  beforeEach(() => {
    vi.mocked(api.submitJob).mockReset();
    vi.mocked(api.restartJob).mockReset();
    localStorage.clear();
  });

  it("prefills bid and time from the job", async () => {
    const detail = jobDetail({ id: 7, bid: 800, est_runtime: 2700, mode: "whole", retries: 1 });
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    expect((screen.getByLabelText("bid") as HTMLInputElement).value).toBe("800");
    expect((screen.getByLabelText("time") as HTMLInputElement).value).toBe("45m");
  });

  it("sends only the bid when just the bid changes", async () => {
    const detail = jobDetail({ id: 7, bid: 800, est_runtime: 2700, mode: "whole", retries: 1 });
    vi.mocked(api.restartJob).mockResolvedValue(submittedJob({ id: 7 }));
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    await fireEvent.input(screen.getByLabelText("bid"), { target: { value: "1200" } });
    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith(7, { bid: 1200 }));
  });

  it("sends {} when nothing changed", async () => {
    const detail = jobDetail({ id: 7, bid: 800, est_runtime: 2700, mode: "whole", retries: 1 });
    vi.mocked(api.restartJob).mockResolvedValue(submittedJob({ id: 7 }));
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith(7, {}));
  });

  it("sends mem when switching from whole to shared", async () => {
    const detail = jobDetail({ id: 7, bid: 800, est_runtime: 2700, mode: "whole", retries: 1 });
    vi.mocked(api.restartJob).mockResolvedValue(submittedJob({ id: 7 }));
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    await fireEvent.click(screen.getByLabelText("shared"));
    await fireEvent.input(screen.getByLabelText("memory size"), { target: { value: "8G" } });
    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith(7, { mem: "8G" }));
  });

  it("sends whole_gpu when switching from shared to whole", async () => {
    const detail = jobDetail({
      id: 7, bid: 800, est_runtime: 2700, mode: "shared", mem_request: 8 * 1024 ** 3, retries: 1,
    });
    vi.mocked(api.restartJob).mockResolvedValue(submittedJob({ id: 7 }));
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    await fireEvent.click(screen.getByLabelText("whole GPU"));
    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith(7, { whole_gpu: true }));
  });

  it("does not resend an untouched shared size prefill", async () => {
    const detail = jobDetail({
      id: 7, bid: 800, est_runtime: 2700, mode: "shared", mem_request: 8 * 1024 ** 3, retries: 1,
    });
    vi.mocked(api.restartJob).mockResolvedValue(submittedJob({ id: 7 }));
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    expect((screen.getByLabelText("memory size") as HTMLInputElement).value).toBe("8.0G");
    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    await waitFor(() => expect(api.restartJob).toHaveBeenCalledWith(7, {}));
  });

  it("shows the API's 409 conflict message and leaves the form open without calling ondone", async () => {
    const detail = jobDetail({ id: 7, bid: 800, est_runtime: 2700, mode: "whole", retries: 1 });
    vi.mocked(api.restartJob).mockRejectedValue(new ApiError(409, "job 7 is running"));
    const ondone = vi.fn();
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone });

    await fireEvent.click(screen.getByRole("button", { name: "Restart" }));

    expect(await screen.findByText("job 7 is running")).toBeInTheDocument();
    expect(ondone).not.toHaveBeenCalled();
  });

  it("shows the command and directory read-only", () => {
    const detail = jobDetail({ id: 7, command: "python train.py", cwd: "/home/you/run" });
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });

    expect(screen.getByText("python train.py")).toBeInTheDocument();
    expect(screen.getByText("/home/you/run")).toBeInTheDocument();
    expect(screen.queryByLabelText("command")).toBeNull();
  });

  it("titles the modal with the job id", () => {
    const detail = jobDetail({ id: 7 });
    render(JobForm, { mode: "restart", job: detail, onclose: noop, ondone: noopDone });
    expect(screen.getByText("Restart #7 with changes")).toBeInTheDocument();
  });
});
