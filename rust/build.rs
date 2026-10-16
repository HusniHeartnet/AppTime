// Windows: give AppTime.exe its cat icon and the version details Task Manager
// and Settings > Apps show ("AppTime", publisher, version).
fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../assets/AppTime.ico")
            .set("FileDescription", "AppTime")
            .set("ProductName", "AppTime")
            .set("CompanyName", "Husni Heartnet")
            .set("LegalCopyright", "Husni Heartnet")
            .set("OriginalFilename", "AppTime.exe")
            .set("InternalName", "AppTime");
        res.compile().expect("Windows resources (icon, version info)");
    }
    println!("cargo:rerun-if-changed=../assets/AppTime.ico");
}
