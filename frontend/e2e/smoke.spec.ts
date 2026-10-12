import { expect, test } from "@playwright/test";

test("首页可访问并显示平台名称", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveTitle("沁爽饮料分销管理平台");
  await expect(page.getByText("沁爽饮料分销管理平台")).toBeVisible();
});
