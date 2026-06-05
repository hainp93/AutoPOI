"""
AutoPOI CLI — Entry point chính

Cách dùng:
    python src/main.py
    python src/main.py --name "Valvoline" --address "1867 College Ave, Elmira, NY 14901"
"""

import sys
import os
import argparse
import logging
from pathlib import Path

# Thêm src vào path
sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint
from rich.prompt import Prompt
from rich.rule import Rule

from src.data_fetcher.geocoder import geocode, build_ve_url, build_gm_url, build_gm_search_url
from src.data_fetcher.gemini_enricher import (
    setup_gemini, setup_gemini_multi, enrich_poi, format_hours_for_display
)
from src.data_fetcher import browser_fetcher

console = Console()

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s | %(name)s | %(message)s"
)


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load config từ YAML file."""
    path = Path(__file__).parent.parent / config_path
    if not path.exists():
        console.print(f"[red]❌ Không tìm thấy config: {path}[/red]")
        console.print(f"[yellow]→ Copy config/config.example.yaml thành config/config.yaml rồi điền API key[/yellow]")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def process_poi(model, name: str, address: str) -> dict:
    """
    Xử lý 1 POI: geocode + enrich data.
    Returns dict với toàn bộ thông tin.
    """
    console.print(f"\n[bold cyan]🔍 Đang xử lý:[/bold cyan] {name} — {address}\n")

    # Step 1: Geocode
    console.print("  [dim]1/2 Geocoding (Nominatim)...[/dim]", end="")
    geo = geocode(name, address)

    if geo:
        console.print(f" [green]✓[/green] ({geo['lat']:.5f}, {geo['lon']:.5f})")
    else:
        console.print(f" [yellow]⚠ Không tìm thấy tọa độ, dùng Google Maps search[/yellow]")

    # Step 2: Gemini enrichment
    console.print("  [dim]2/2 Gemini Search (có thể mất 10-20 giây)...[/dim]", end="")
    enriched = enrich_poi(model, name, address)
    console.print(f" [green]✓[/green]")

    # Kết hợp kết quả
    result = {
        "name": name,
        "address": address,
        **enriched,
    }

    if geo:
        result["lat"] = geo["lat"]
        result["lon"] = geo["lon"]
        result["ve_url"] = build_ve_url(geo["lat"], geo["lon"])
        result["gm_url"] = build_gm_url(geo["lat"], geo["lon"])
    else:
        result["lat"] = None
        result["lon"] = None
        result["ve_url"] = None
        result["gm_url"] = build_gm_search_url(name, address)

    return result


def display_result(result: dict):
    """Hiển thị kết quả đẹp trong terminal."""
    console.print()
    console.rule(f"[bold green]📍 {result['name']}[/bold green]")
    console.print(f"[dim]{result['address']}[/dim]\n")

    # Main table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Field", style="bold yellow", width=20)
    table.add_column("Value")

    # Coordinates
    if result.get("lat"):
        table.add_row("🗺  Tọa độ", f"{result['lat']:.6f}, {result['lon']:.6f}")
    
    # Category
    if result.get("category"):
        table.add_row("🏷  Category", result["category"])

    # Status
    if result.get("is_closed"):
        table.add_row("⛔ Trạng thái", "[red]ĐÃ ĐÓNG CỬA[/red]")
    else:
        table.add_row("✅ Trạng thái", "[green]Đang hoạt động[/green]")

    # Opening Hours
    oh = result.get("opening_hours", "")
    oh_display = format_hours_for_display(oh)
    table.add_row("🕐 Opening Hours", oh_display or "[dim](không tìm thấy)[/dim]")
    if oh:
        table.add_row("   [dim]VE format[/dim]", f"[cyan]{oh}[/cyan]")
    if result.get("opening_hours_source"):
        table.add_row("   [dim]Nguồn OH[/dim]", f"[dim]{result['opening_hours_source'][:80]}[/dim]")

    # Opening Date
    od = result.get("opening_date", "")
    table.add_row("📅 Opening Date", f"[magenta]{od}[/magenta]" if od else "[dim](không tìm thấy)[/dim]")
    if result.get("opening_date_source"):
        table.add_row("   [dim]Nguồn OD[/dim]", f"[dim]{result['opening_date_source'][:80]}[/dim]")

    # Closing Date (nếu có)
    if result.get("is_closed") and result.get("closing_date"):
        table.add_row("🚫 Closing Date", f"[red]{result['closing_date']}[/red]")
        if result.get("closing_date_source"):
            table.add_row("   [dim]Nguồn CD[/dim]", f"[dim]{result['closing_date_source'][:80]}[/dim]")

    # Notes
    if result.get("status_note"):
        table.add_row("📝 Ghi chú", f"[dim]{result['status_note']}[/dim]")

    console.print(table)

    # Links
    console.print()
    console.print("[bold]🔗 Links:[/bold]")
    console.print(f"  [bold]Google Maps:[/bold] [link={result['gm_url']}]{result['gm_url']}[/link]")

    if result.get("ve_url"):
        console.print(f"  [bold]Venue Editor:[/bold] [link={result['ve_url']}]{result['ve_url']}[/link]")
    else:
        console.print("  [bold]Venue Editor:[/bold] [yellow]Cần geocode thủ công để lấy tọa độ[/yellow]")

    # Copy-paste section
    console.print()
    console.print(Panel(
        f"[bold]Tags để dán vào VE:[/bold]\n\n"
        f"[cyan]opening_hours:pl[/cyan]  →  [white]{oh or '(điền thủ công)'}[/white]\n"
        f"[cyan]date_opened:pl[/cyan]    →  [white]{od or '(điền thủ công)'}[/white]"
        + (f"\n[cyan]is_closed:pl[/cyan]      →  [white]yes[/white]\n[cyan]date_closed:pl[/cyan]    →  [white]{result.get('closing_date', '')}[/white]" if result.get("is_closed") else ""),
        title="📋 Copy-paste ready",
        border_style="cyan"
    ))


def interactive_mode(model):
    """Chế độ tương tác: nhập nhiều POI liên tiếp."""
    console.print(Panel(
        "[bold green]AutoPOI Data Assistant[/bold green]\n"
        "[dim]Nhập tên + địa chỉ POI để tự động tìm thông tin[/dim]\n"
        "[dim]Gõ 'quit' hoặc Ctrl+C để thoát[/dim]",
        border_style="green"
    ))

    while True:
        console.print()
        console.rule("[dim]POI mới[/dim]")
        
        name = Prompt.ask("[bold yellow]Tên địa điểm[/bold yellow]").strip()
        if name.lower() in ("quit", "exit", "q"):
            console.print("\n[dim]Tạm biệt! 👋[/dim]")
            break

        address = Prompt.ask("[bold yellow]Địa chỉ (US)[/bold yellow]").strip()
        if not name or not address:
            console.print("[red]Cần nhập cả tên và địa chỉ![/red]")
            continue

        try:
            result = process_poi(model, name, address)
            display_result(result)
        except KeyboardInterrupt:
            console.print("\n[yellow]Bỏ qua POI này[/yellow]")
            continue


def main():
    parser = argparse.ArgumentParser(description="AutoPOI — Tự động tìm thông tin POI")
    parser.add_argument("--name", type=str, help="Tên địa điểm")
    parser.add_argument("--address", type=str, help="Địa chỉ (US)")
    parser.add_argument("--config", type=str, default="config/config.yaml")
    parser.add_argument("--debug", action="store_true", help="Bật debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load config
    config = load_config(args.config)
    gemini_cfg    = config.get("gemini", {})
    gemini_model_name = gemini_cfg.get("model", "gemini-2.5-flash")
    gemini_api_keys   = gemini_cfg.get("api_keys", [])
    gemini_api_key    = gemini_cfg.get("api_key", "")

    if gemini_api_keys and any(k and not k.startswith("YOUR_") for k in gemini_api_keys):
        # Multi-key mode
        valid_keys = [k for k in gemini_api_keys if k and not k.startswith("YOUR_")]
        model = setup_gemini_multi(valid_keys, gemini_model_name)
        console.print(f"[green]✓ Multi-key mode:[/green] {len(valid_keys)} key(s) — mỗi step dùng 1 key riêng")
    elif gemini_api_key and gemini_api_key != "YOUR_GEMINI_API_KEY":
        model = setup_gemini(gemini_api_key, gemini_model_name)
        console.print("[yellow]⚠ Single-key mode:[/yellow] 1 key dùng cho cả 3 step")
    else:
        console.print("[red]❌ Chưa cấu hình Gemini API key trong config.yaml![/red]")
        console.print("   Điền [cyan]api_key[/cyan] (1 key) hoặc [cyan]api_keys[/cyan] (list 3 key) trong section [gemini]")
        sys.exit(1)

    # Setup Chrome browser (optional — dùng cho Step 2c)
    chrome_cfg = config.get("chrome", {})
    chrome_path = chrome_cfg.get("profile_path", "")
    if chrome_path and chrome_path not in ("auto", ""):
        chrome_dir = chrome_cfg.get("profile_dir", "Default")
        browser_fetcher.setup_browser(
            profile_path=chrome_path,
            profile_dir=chrome_dir,
            offscreen_x=chrome_cfg.get("offscreen_x", -3000),
            page_wait=chrome_cfg.get("page_wait", 3),
        )
        console.print(f"[green]✓ Chrome browser:[/green] profile '{chrome_dir}' (off-screen, Step 2c fallback)")
    elif chrome_path == "auto":
        import os
        auto_path = f"C:\\Users\\{os.getenv('USERNAME','')}\\AppData\\Local\\Google\\Chrome\\User Data"
        browser_fetcher.setup_browser(profile_path=auto_path)
        console.print(f"[green]✓ Chrome browser (auto):[/green] {auto_path}")
    else:
        console.print("[dim]  Chrome browser: không cấu hình (bỏ qua Step 2c)[/dim]")

    # Single mode hoặc interactive mode
    if args.name and args.address:
        result = process_poi(model, args.name, args.address)
        display_result(result)
    else:
        interactive_mode(model)


if __name__ == "__main__":
    main()
