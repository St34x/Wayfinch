// SPDX-License-Identifier: Apache-2.0
// Glasses session and the BLE driver (items 9 and 10).
plugins {
    id("com.android.library")
}

android {
    namespace = "app.hudcompanion.glasses"
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

dependencies {
    implementation(project(":protocol"))
    implementation(project(":hud"))
    implementation(project(":input"))
    implementation(project(":core"))
    implementation(libs.coroutines.android)
}

// -PtestMinSdk reaches the debug build type only (root build.gradle.kts).
androidComponents.beforeVariants(androidComponents.selector().withBuildType("debug")) {
    it.minSdk = rootProject.extra["androidDebugMinSdk"] as Int
}
