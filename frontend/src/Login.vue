<script setup>
import { ref } from "vue";
import Icon from "./Icon.vue";
import ThemeSwitcher from "./ThemeSwitcher.vue";
const username = ref(""),
  password = ref(""),
  busy = ref(false),
  error = ref(""),
  visible = ref(false);
async function signIn() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/json",
      },
      body: JSON.stringify({
        username: username.value,
        password: password.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || response.statusText);
    window.location.replace("/");
  } catch (reason) {
    error.value = reason.message || "Sign in failed";
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="login-layout">
    <div class="login-form-side">
      <div class="login-appearance"><ThemeSwitcher /></div>
      <div class="login-form-container">
        <span class="login-mobile-brand"
          ><Icon name="Cpu" :size="22" /> GPU Watcher</span
        ><span class="login-welcome-icon"
          ><Icon name="Command" :size="25"
        /></span>
        <h2>Welcome back.</h2>
        <p>Sign in to your compute workspace.</p>
        <form id="loginForm" @submit.prevent="signIn">
          <label
            ><span>Username</span
            ><input
              v-model="username"
              name="username"
              autocomplete="username"
              placeholder="Your username"
              required
              autofocus /></label
          ><label
            ><span>Password</span>
            <div class="password-field">
              <input
                aria-label="Password"
                v-model="password"
                name="password"
                :type="visible ? 'text' : 'password'"
                autocomplete="current-password"
                placeholder="Enter your password"
                required
              /><button
                type="button"
                :aria-label="visible ? 'Hide password' : 'Show password'"
                :aria-pressed="visible"
                @click="visible = !visible"
              >
                <Icon :name="visible ? 'EyeOff' : 'Eye'" :size="17" />
              </button></div
          ></label>
          <p v-if="error" class="login-error" role="alert">{{ error }}</p>
          <button type="submit" :disabled="busy">
            {{ busy ? "Signing in…" : "Sign in"
            }}<Icon
              :name="busy ? 'RefreshCw' : 'ArrowRight'"
              :class="{ spinning: busy }"
              :size="17"
            />
          </button>
        </form>
      </div>
    </div>
  </div>
</template>
