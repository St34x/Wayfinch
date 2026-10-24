// SPDX-License-Identifier: Apache-2.0
// The phone app's screens: launcher MainActivity, bottom bar, tabs (T1.37a).
plugins {
    id("com.android.library")
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "app.hudcompanion.shell"
    compileSdk = rootProject.extra["androidSdk"] as Int
    buildToolsVersion = rootProject.extra["androidBuildTools"] as String
    defaultConfig {
        minSdk = rootProject.extra["androidSdk"] as Int
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }
    buildFeatures { compose = true }
}

dependencies {
    implementation(project(":module-api"))
    implementation(project(":ui-tokens"))
    implementation(project(":core"))
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.material3)
    implementation(libs.navigation.compose)
    implementation(libs.coroutines.android)
}

// -PtestMinSdk reaches the debug build type only (root build.gradle.kts).
androidComponents.beforeVariants(androidComponents.selector().withBuildType("debug")) {
    it.minSdk = rootProject.extra["androidDebugMinSdk"] as Int
}
