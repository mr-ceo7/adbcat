from adbcat.detect import detect_project


def write(root, rel, text=""):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_kotlin_dsl_with_version_catalog_and_subdir(tmp_path):
    write(tmp_path, "settings.gradle.kts")
    write(tmp_path, "build.gradle.kts", "plugins {\n alias(libs.plugins.android.application) apply false\n}")
    write(tmp_path, "app/build.gradle.kts",
          'plugins { alias(libs.plugins.android.application) }\n'
          'android {\n namespace = "com.a.ns"\n defaultConfig {\n  applicationId = "com.a.app"\n }\n}')
    p = detect_project(tmp_path / "app")
    assert p.root == tmp_path and p.packages == ["com.a.app"]


def test_groovy_suffix_flavors(tmp_path):
    write(tmp_path, "settings.gradle")
    write(tmp_path, "app/build.gradle",
          "apply plugin: 'com.android.application'\n"
          "android { defaultConfig { applicationId 'com.b.app' }\n"
          " buildTypes { debug { applicationIdSuffix '.debug' } }\n"
          " productFlavors { pro { applicationId \"com.b.pro\" } } }")
    assert detect_project(tmp_path).packages == [
        "com.b.app", "com.b.pro", "com.b.app.debug", "com.b.pro.debug"]


def test_namespace_and_manifest_fallbacks(tmp_path):
    write(tmp_path, "settings.gradle.kts")
    write(tmp_path, "one/build.gradle.kts", 'plugins { id("com.android.application") }\nandroid { namespace = "com.c.one" }')
    write(tmp_path, "two/build.gradle", "plugins { id 'com.android.application' }")
    write(tmp_path, "two/src/main/AndroidManifest.xml", '<manifest xmlns:android="x"\n package="com.c.two">')
    assert sorted(detect_project(tmp_path).packages) == ["com.c.one", "com.c.two"]


def test_flutter_layout(tmp_path):
    write(tmp_path, "pubspec.yaml")
    write(tmp_path, "android/settings.gradle")
    write(tmp_path, "android/app/build.gradle", 'android { defaultConfig { applicationId "com.d.flutter" } }')
    assert detect_project(tmp_path).packages == ["com.d.flutter"]


def test_not_an_app(tmp_path):
    assert detect_project(tmp_path) is None
    write(tmp_path, "settings.gradle.kts")
    write(tmp_path, "lib/build.gradle.kts",
          'plugins { id("com.android.library") }\nandroid { namespace = "com.e.lib" }\n'
          '// applicationId = "com.commented.out"')
    assert detect_project(tmp_path) is None
