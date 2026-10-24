// SPDX-License-Identifier: Apache-2.0
// Audio pipeline (T1.166); empty until then.
plugins {
    id("com.android.library")
}

android {
    namespace = "app.hudcompanion.audio"
    compileSdk = rootProject.extra["androidSdk"] as Int
    buildToolsVersion = rootProject.extra["androidBuildTools"] as String
    defaultConfig {
        minSdk = rootProject.extra["androidSdk"] as Int
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
}

// -PtestMinSdk reaches the debug build type only (root build.gradle.kts).
androidComponents.beforeVariants(androidComponents.selector().withBuildType("debug")) {
    it.minSdk = rootProject.extra["androidDebugMinSdk"] as Int
}
