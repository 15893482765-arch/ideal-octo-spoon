export default function App() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <div className="mx-6 max-w-lg rounded-lg border border-slate-200 bg-white p-10 text-center shadow-sm">
        <h1 className="text-2xl font-semibold" style={{ color: "#0F766E" }}>
          沁爽饮料分销管理平台
        </h1>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          工程骨架已就绪（T01）。业务页面将按任务卡逐步交付：
          账号登录、商家中心、合同与冰柜、巡访排班、经营报表。
        </p>
        <p className="mt-2 text-xs text-slate-400">下一步：T02 数据库建模与迁移</p>
      </div>
    </div>
  );
}
