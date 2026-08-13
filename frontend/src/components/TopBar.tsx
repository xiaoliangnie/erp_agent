import { useEffect } from "react";
import type { ReactNode } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { NAV_ITEMS } from "../routes";

interface TopBarProps {
  title: string;
  sub?: ReactNode;
}

export function TopBar({ title, sub }: TopBarProps) {
  const { pathname } = useLocation();
  const [params] = useSearchParams();
  const year = params.get("year");

  // 单页应用换路由不会自己改标题，多标签页并排时靠它区分。
  useEffect(() => {
    document.title = `${title} · 蜀黍家`;
  }, [title]);

  return (
    <header className="topbar">
      <h1>{title}</h1>
      {sub ? <div className="sub">{sub}</div> : null}
      <div className="spacer" />
      <nav aria-label="页面导航">
        {NAV_ITEMS.filter((item) => item.path !== pathname).map((item) => (
          <Link key={item.path} to={item.keepsYear && year ? `${item.path}?year=${encodeURIComponent(year)}` : item.path}>
            {item.label}
          </Link>
        ))}
      </nav>
    </header>
  );
}
