import { render } from "@testing-library/svelte";
import { describe, expect, it } from "vitest";
import Peek from "../components/Peek.svelte";

describe("Peek", () => {
  it("renders the image at the manifest URL when a custom peek image exists", () => {
    const { container } = render(Peek, { src: "/mascot/peek.webp" });
    const img = container.querySelector<HTMLImageElement>("img.peek");
    expect(img).not.toBeNull();
    expect(img!.src).toContain("/mascot/peek.webp");
    expect(img!.alt).toBe("");
    expect(img!.getAttribute("aria-hidden")).toBe("true");
  });

  it("renders nothing when there is no custom peek image", () => {
    const { container } = render(Peek, { src: null });
    expect(container.querySelector("img")).toBeNull();
  });
});
