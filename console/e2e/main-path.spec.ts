import { expect, test, type Locator, type Page } from "@playwright/test";

// What scripts/seed_demo.sh puts on the stack. tests/unit/test_console_e2e.py keeps these names
// in step with the script, so a rename on one side fails a unit test and not this one.
const TOKEN = process.env.LONGHAUL_ADMIN_TOKEN || "demo-admin-token";
const SHIPPED = { title: "Fix add() so it sums", repo: "demo/calc-shipped" };
const STUCK = { title: "Fix add() after a hint" };
const PLAN = { title: "Fix add() after a plan review" };
const LESSONS = [
  "Run the whole suite with python -m pytest -q before you submit a fix.",
  "calc.add and calc.multiply both live in src/calc/__init__.py.",
];

// The page polls every 10 seconds, so a change made by the stack shows within 15.
const SEEN = { timeout: 15_000 };
// A task still plans or sets up its workspace for a while after the seed returns to the pause.
const SETTLED = { timeout: 60_000 };

function phase(page: Page): Locator {
  return page.getByRole("region", { name: "Task summary" }).locator("dt:text-is('Phase') + dd");
}

// The health dot is a status as well, so the toast is found by its text.
function toast(page: Page, text: string): Locator {
  return page.getByRole("status").filter({ hasText: text });
}

// The list row of a task, found by the link to it.
function rowOf(page: Page, title: string): Locator {
  return page.getByRole("row").filter({ has: page.getByRole("link", { name: title, exact: true }) });
}

async function openTask(page: Page, title: string): Promise<void> {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Tasks" }).click();
  await page.getByRole("link", { name: title, exact: true }).click();
  await expect(page.getByRole("heading", { level: 1, name: title })).toBeVisible();
}

test("the main path of the console", async ({ page }) => {
  await test.step("log in and see the three tasks", async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await page.getByLabel("Admin token").fill(TOKEN);
    await page.getByRole("button", { name: "Sign in" }).click();

    await expect(page.getByRole("heading", { level: 1, name: "Tasks" })).toBeVisible();
    await expect(page.locator("tbody tr")).toHaveCount(3);
    for (const { title } of [SHIPPED, STUCK, PLAN]) {
      await expect(page.getByRole("link", { name: title, exact: true })).toBeVisible();
    }
  });

  await test.step("see each task in the list as it is", async () => {
    // The list reads the row of the task, which the workflow keeps current. A row nobody updates
    // shows every task as queued, with no turns and no outcome, and nothing under Needs you.
    const shipped = rowOf(page, SHIPPED.title);
    await expect(shipped).toContainText("done", SETTLED);
    await expect(shipped).toContainText("pr_opened");
    await expect(shipped.locator("td").nth(5)).toHaveText(/^[1-9]/);
    await expect(shipped.locator("td").last()).toHaveText("");

    const stuck = rowOf(page, STUCK.title);
    await expect(stuck).toContainText("paused", SETTLED);
    await expect(stuck.locator("td").last()).toHaveText("Paused");
    await expect(stuck.locator("td").nth(5)).toHaveText(/^[1-9]/);

    const plan = rowOf(page, PLAN.title);
    await expect(plan).toContainText("awaiting_plan_approval", SETTLED);
    await expect(plan.locator("td").last()).toHaveText("Approve the plan");

    // The box follows the address, so it changes a moment after the click and not during it.
    await page.getByLabel("Needs you").click();
    await expect(page.getByLabel("Needs you")).toBeChecked();
    await expect(page.locator("tbody tr")).toHaveCount(2);
    await expect(page.getByRole("link", { name: SHIPPED.title, exact: true })).toHaveCount(0);
    await page.getByLabel("Needs you").click();
    await expect(page.getByLabel("Needs you")).not.toBeChecked();
    await expect(page.locator("tbody tr")).toHaveCount(3);
  });

  await test.step("send a hint to the stuck task and see it code again", async () => {
    await openTask(page, STUCK.title);
    await expect(phase(page)).toHaveText("paused", SETTLED);

    await page.getByLabel("Hint", { exact: true }).fill("Yes, change the operator in add() from - to +.");
    await page.getByRole("button", { name: "Send hint" }).click();
    await expect(toast(page, "Hint sent.")).toBeVisible();
    await expect(phase(page)).toHaveText("coding", SEEN);
  });

  await test.step("approve the plan", async () => {
    await openTask(page, PLAN.title);
    await expect(phase(page)).toHaveText("awaiting_plan_approval", SETTLED);

    await page.getByRole("button", { name: "Approve plan" }).click();
    await expect(toast(page, "Plan approved.")).toBeVisible();
    await expect(phase(page)).not.toHaveText("awaiting_plan_approval", SEEN);
    await expect(page.getByRole("button", { name: "Approve plan" })).toHaveCount(0);
  });

  await test.step("approve a lesson", async () => {
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Memory" }).click();
    await page.getByLabel("Repo").selectOption(SHIPPED.repo);
    await page.getByRole("tab", { name: /^Candidates/ }).click();
    await expect(page.getByRole("tab", { name: "Candidates (2)" })).toBeVisible();

    const lesson = page.getByRole("listitem").filter({ hasText: LESSONS[0]! });
    await lesson.getByRole("button", { name: "Approve" }).click();
    await expect(page.getByRole("tab", { name: "Candidates (1)" })).toBeVisible();

    await page.getByRole("tab", { name: /^Approved/ }).click();
    await expect(page.getByRole("listitem").filter({ hasText: LESSONS[0]! })).toBeVisible();
    // The other lesson is still waiting for a decision.
    await expect(page.getByRole("listitem").filter({ hasText: LESSONS[1]! })).toHaveCount(0);
  });
});
