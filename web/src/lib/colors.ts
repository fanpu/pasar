export const JOB_COLORS = ["#3b8fd9", "#d9577f", "#8a63d2", "#23a47a", "#c9761f"];

export function jobColor(id: number): string {
  return JOB_COLORS[((id % 5) + 5) % 5];
}
