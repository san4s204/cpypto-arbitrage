"""
Telegram Manager Microservice for Crypto Arbitrage Bot.
Handles Telegram bot integration for trade confirmations and notifications.
"""
import asyncio
import logging
import time
from datetime import datetime
import json
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import uvicorn
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from common.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID, TG_MANAGER_PORT
)
from common.redis_utils import (
    get_cached_opportunity, update_system_metric
)
from common.database import (
    get_session, update_opportunity_status,
    get_recent_opportunities, get_daily_pnl
)

# Setup logging
logger = logging.getLogger("tg_manager")

# Initialize FastAPI app
app = FastAPI(title="Telegram Manager Service")

# Initialize Telegram bot
bot_application = None
pending_confirmations = {}

async def setup_telegram_bot():
    """Initialize and setup Telegram bot."""
    global bot_application
    
    try:
        # Create application
        bot_application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Add handlers
        bot_application.add_handler(CommandHandler("start", start_command))
        bot_application.add_handler(CommandHandler("help", help_command))
        bot_application.add_handler(CommandHandler("status", status_command))
        bot_application.add_handler(CommandHandler("balance", balance_command))
        bot_application.add_handler(CommandHandler("pnl", pnl_command))
        bot_application.add_handler(CallbackQueryHandler(button_callback))
        
        # Start bot
        await bot_application.initialize()
        await bot_application.start()
        
        logger.info("Telegram bot started successfully")
        
        # Send startup notification
        await send_admin_message("🤖 Crypto Arbitrage Bot started and ready for operation.")
        
        return True
    
    except Exception as e:
        logger.error(f"Error setting up Telegram bot: {str(e)}")
        return False

async def shutdown_telegram_bot():
    """Shutdown Telegram bot."""
    global bot_application
    
    if bot_application:
        try:
            await bot_application.stop()
            await bot_application.shutdown()
            logger.info("Telegram bot shutdown successfully")
        except Exception as e:
            logger.error(f"Error shutting down Telegram bot: {str(e)}")

async def send_admin_message(text, reply_markup=None):
    """Send message to admin chat."""
    global bot_application
    
    if not bot_application:
        logger.error("Telegram bot not initialized")
        return False
    
    try:
        await bot_application.bot.send_message(
            chat_id=TELEGRAM_ADMIN_CHAT_ID,
            text=text,
            reply_markup=reply_markup
        )
        return True
    
    except Exception as e:
        logger.error(f"Error sending admin message: {str(e)}")
        return False

async def send_trade_confirmation_request(opportunity_id, pair, buy_exchange, sell_exchange, profit_margin, additional_funds_pct):
    """Send trade confirmation request to admin."""
    # Create inline keyboard
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{opportunity_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{opportunity_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Format message
    message = (
        f"⚠️ Нужно +{additional_funds_pct:.1f}% средств для сделки\n"
        f"Пара: {pair}\n"
        f"Биржи: {buy_exchange} → {sell_exchange}\n"
        f"Маржа после комиссий: {profit_margin:.2f}%\n"
        f"Подтвердить? (✅ / ❌)"
    )
    
    # Send message
    result = await send_admin_message(message, reply_markup)
    
    if result:
        # Track pending confirmation
        pending_confirmations[opportunity_id] = {
            "pair": pair,
            "buy_exchange": buy_exchange,
            "sell_exchange": sell_exchange,
            "profit_margin": profit_margin,
            "additional_funds_pct": additional_funds_pct,
            "timestamp": datetime.utcnow().isoformat(),
            "status": "PENDING"
        }
    
    return result

async def send_trade_execution_result(opportunity_id, pair, pnl, fees, cycle_time):
    """Send trade execution result to admin."""
    # Format message
    message = (
        f"✅ Арбитраж выполнен\n"
        f"Пара: {pair}\n"
        f"PnL: +{pnl:.2f}%\n"
        f"Комиссии: {fees:.2f}%\n"
        f"Время цикла: {cycle_time} сек"
    )
    
    # Send message
    return await send_admin_message(message)

async def send_insufficient_funds_notification(opportunity_id, pair):
    """Send insufficient funds notification to admin."""
    # Format message
    message = f"❌ Недостаточно средств — сделка отменена\nПара: {pair}\nID: {opportunity_id}"
    
    # Send message
    return await send_admin_message(message)

async def send_opportunity_expired_notification(opportunity_id, pair):
    """Send opportunity expired notification to admin."""
    # Format message
    message = f"⏱️ Возможность истекла — спред упал ниже порога\nПара: {pair}\nID: {opportunity_id}"
    
    # Send message
    return await send_admin_message(message)

# Telegram command handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is for admin use only.")
        return
    
    await update.message.reply_text(
        "Welcome to Crypto Arbitrage Bot Admin Interface!\n\n"
        "Use /help to see available commands."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is for admin use only.")
        return
    
    await update.message.reply_text(
        "Available commands:\n\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/status - Check bot status\n"
        "/balance - Check exchange balances\n"
        "/pnl - Check daily PnL"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is for admin use only.")
        return
    
    # Get recent opportunities
    async with AsyncSession() as session:
        opportunities = await get_recent_opportunities(session, 5)
    
    # Format message
    message = "🤖 Crypto Arbitrage Bot Status\n\n"
    
    if opportunities:
        message += "Recent opportunities:\n"
        for opp in opportunities:
            message += f"- {opp.pair}: {opp.profit_margin:.2f}% ({opp.status})\n"
    else:
        message += "No recent opportunities found.\n"
    
    message += "\nBot is running normally."
    
    await update.message.reply_text(message)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /balance command."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is for admin use only.")
        return
    
    # This would normally fetch balances from exchanges
    # For now, we'll just return a placeholder message
    await update.message.reply_text(
        "💰 Exchange Balances\n\n"
        "OKX:\n"
        "- BTC: 0.1\n"
        "- ETH: 1.5\n"
        "- USDT: 5000\n\n"
        "Bybit:\n"
        "- BTC: 0.05\n"
        "- ETH: 2.0\n"
        "- USDT: 3000\n\n"
        "HTX:\n"
        "- BTC: 0.08\n"
        "- ETH: 1.0\n"
        "- USDT: 4000"
    )

async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pnl command."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.message.reply_text("Unauthorized access. This bot is for admin use only.")
        return
    
    # Get daily PnL
    async with AsyncSession() as session:
        pnl = await get_daily_pnl(session)
    
    # Format message
    message = f"📊 Daily PnL: ${pnl:.2f}\n\n"
    
    # This would normally include more detailed PnL breakdown
    # For now, we'll just return a placeholder message
    message += "Performance by pair:\n"
    message += "- BTC/USDT: +$50.25\n"
    message += "- ETH/USDT: +$35.10\n"
    message += "- SOL/USDT: +$15.75\n\n"
    message += "Performance by exchange:\n"
    message += "- OKX: +$40.50\n"
    message += "- Bybit: +$35.25\n"
    message += "- HTX: +$25.35"
    
    await update.message.reply_text(message)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button callbacks."""
    # Check if user is admin
    if str(update.effective_chat.id) != TELEGRAM_ADMIN_CHAT_ID:
        await update.callback_query.answer("Unauthorized access.")
        return
    
    query = update.callback_query
    await query.answer()
    
    # Extract callback data
    data = query.data
    
    if data.startswith("confirm_"):
        # Extract opportunity ID
        opportunity_id = int(data.split("_")[1])
        
        # Process confirmation
        await process_confirmation(opportunity_id, True)
        
        # Update message
        await query.edit_message_text(
            text=f"✅ Trade confirmed for opportunity {opportunity_id}. Processing..."
        )
    
    elif data.startswith("reject_"):
        # Extract opportunity ID
        opportunity_id = int(data.split("_")[1])
        
        # Process rejection
        await process_confirmation(opportunity_id, False)
        
        # Update message
        await query.edit_message_text(
            text=f"❌ Trade rejected for opportunity {opportunity_id}."
        )

async def process_confirmation(opportunity_id, confirmed):
    """Process trade confirmation."""
    if opportunity_id not in pending_confirmations:
        logger.error(f"Opportunity {opportunity_id} not found in pending confirmations")
        return False
    
    # Get opportunity details
    opportunity = pending_confirmations[opportunity_id]
    
    # Update status
    if confirmed:
        # Verify opportunity is still valid
        # This would normally check with ArbEngine to recalculate spread
        # For now, we'll just assume it's still valid
        
        # Update status
        opportunity["status"] = "CONFIRMED"
        
        # Update database status
        async with AsyncSession() as session:
            await update_opportunity_status(session, opportunity_id, "EXECUTING")
        
        # Notify execution service
        # This would normally call the execution service API
        # For now, we'll just log it
        logger.info(f"Executing opportunity {opportunity_id}")
        
        # Simulate execution result
        await asyncio.sleep(2)
        await send_trade_execution_result(
            opportunity_id=opportunity_id,
            pair=opportunity["pair"],
            pnl=opportunity["profit_margin"],
            fees=0.08,
            cycle_time=27
        )
    
    else:
        # Update status
        opportunity["status"] = "REJECTED"
        
        # Update database status
        async with AsyncSession() as session:
            await update_opportunity_status(session, opportunity_id, "CANCELED")
    
    return True

@app.on_event("startup")
async def startup_event():
    """Initialize and start Telegram bot."""
    await setup_telegram_bot()

@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown Telegram bot."""
    await shutdown_telegram_bot()

@app.post("/confirm_trade")
async def confirm_trade_endpoint(
    opportunity_id: int,
    pair: str,
    buy_exchange: str,
    sell_exchange: str,
    profit_margin: float,
    additional_funds_pct: float
):
    """Request trade confirmation from admin."""
    result = await send_trade_confirmation_request(
        opportunity_id=opportunity_id,
        pair=pair,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        profit_margin=profit_margin,
        additional_funds_pct=additional_funds_pct
    )
    
    if result:
        return {
            "status": "pending",
            "opportunity_id": opportunity_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to send confirmation request")

@app.post("/notify_execution")
async def notify_execution_endpoint(
    opportunity_id: int,
    pair: str,
    pnl: float,
    fees: float,
    cycle_time: int
):
    """Notify admin about trade execution result."""
    result = await send_trade_execution_result(
        opportunity_id=opportunity_id,
        pair=pair,
        pnl=pnl,
        fees=fees,
        cycle_time=cycle_time
    )
    
    if result:
        return {
            "status": "sent",
            "opportunity_id": opportunity_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to send execution notification")

@app.post("/notify_insufficient_funds")
async def notify_insufficient_funds_endpoint(
    opportunity_id: int,
    pair: str
):
    """Notify admin about insufficient funds."""
    result = await send_insufficient_funds_notification(
        opportunity_id=opportunity_id,
        pair=pair
    )
    
    if result:
        return {
            "status": "sent",
            "opportunity_id": opportunity_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to send insufficient funds notification")

@app.post("/notify_opportunity_expired")
async def notify_opportunity_expired_endpoint(
    opportunity_id: int,
    pair: str
):
    """Notify admin about expired opportunity."""
    result = await send_opportunity_expired_notification(
        opportunity_id=opportunity_id,
        pair=pair
    )
    
    if result:
        return {
            "status": "sent",
            "opportunity_id": opportunity_id,
            "timestamp": datetime.utcnow().isoformat()
        }
    else:
        raise HTTPException(status_code=500, detail="Failed to send opportunity expired notification")

@app.get("/pending_confirmations")
async def get_pending_confirmations():
    """Get all pending trade confirmations."""
    return {
        "confirmations": pending_confirmations,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/status")
async def get_status():
    """Get service status."""
    return {
        "status": "running",
        "pending_confirmations": len(pending_confirmations),
        "telegram_bot_running": bot_application is not None,
        "timestamp": datetime.utcnow().isoformat()
    }

if __name__ == "__main__":
    uvicorn.run("tg_manager.main:app", host="0.0.0.0", port=TG_MANAGER_PORT, reload=False)
