//! The tray icon (Windows) / menu bar icon (macOS) and its menu.

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use tao::event::{Event, StartCause};
use tao::event_loop::{ControlFlow, EventLoopBuilder};
use tray_icon::menu::{CheckMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};

use crate::{app, platform};

enum TrayEvent {
    Menu(MenuEvent),
    Icon(TrayIconEvent),
}

fn tray_image() -> Icon {
    let decoder = png::Decoder::new(&include_bytes!("../../assets/tray.png")[..]);
    let mut reader = decoder.read_info().expect("tray.png");
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).expect("tray.png");
    Icon::from_rgba(buf[..info.buffer_size()].to_vec(), info.width, info.height).expect("tray icon")
}

pub fn run() -> ! {
    #[allow(unused_mut)]
    let mut event_loop = EventLoopBuilder::<TrayEvent>::with_user_event().build();
    #[cfg(target_os = "macos")]
    {
        use tao::platform::macos::{ActivationPolicy, EventLoopExtMacOS};
        event_loop.set_activation_policy(ActivationPolicy::Accessory); // menu bar only, no Dock icon
    }

    let proxy = event_loop.create_proxy();
    MenuEvent::set_event_handler(Some(move |e| {
        proxy.send_event(TrayEvent::Menu(e)).ok();
    }));
    let proxy = event_loop.create_proxy();
    TrayIconEvent::set_event_handler(Some(move |e| {
        proxy.send_event(TrayEvent::Icon(e)).ok();
    }));

    let open = MenuItem::new("Open dashboard", true, None);
    let pause = CheckMenuItem::new("Pause tracking", true, false, None);
    let startup = CheckMenuItem::new(platform::STARTUP_LABEL, true, platform::startup_enabled(), None);
    let quit = MenuItem::new("Quit", true, None);
    let menu = Menu::new();
    menu.append_items(&[&open, &pause, &startup, &PredefinedMenuItem::separator(), &quit]).ok();

    let mut tray: Option<TrayIcon> = None;
    event_loop.run(move |event, _, control_flow| {
        // Wake now and then to keep the menu's checkmarks in step with the dashboard.
        *control_flow = ControlFlow::WaitUntil(Instant::now() + Duration::from_secs(2));
        match event {
            Event::NewEvents(StartCause::Init) => {
                // macOS needs the icon created after the event loop has started.
                tray = TrayIconBuilder::new()
                    .with_menu(Box::new(menu.clone()))
                    .with_menu_on_left_click(cfg!(target_os = "macos"))
                    .with_tooltip("AppTime")
                    .with_icon(tray_image())
                    .build()
                    .ok();
            }
            Event::UserEvent(TrayEvent::Icon(TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            })) => app().window.open(),
            Event::UserEvent(TrayEvent::Menu(e)) => {
                if e.id == open.id() {
                    app().window.open();
                } else if e.id == pause.id() {
                    app().tracker.paused.store(pause.is_checked(), Ordering::Relaxed);
                } else if e.id == startup.id() {
                    crate::set_startup(startup.is_checked());
                } else if e.id == quit.id() {
                    tray.take();
                    crate::quit_now();
                }
            }
            _ => {
                pause.set_checked(app().tracker.paused.load(Ordering::Relaxed));
                startup.set_checked(platform::startup_enabled());
            }
        }
    })
}
